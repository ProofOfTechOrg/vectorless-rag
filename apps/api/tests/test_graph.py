from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import ProgrammingError

from vectorless_rag.config import Settings
from vectorless_rag.graph import GraphRunner, append_inline_citations, bounded_history
from vectorless_rag.models import QueryRun, RunRoute
from vectorless_rag.schemas import ChatRequest, RouteDecision, SQLPlan, SynthesisResult
from vectorless_rag.sql_guard import SQLResult


def _run(question: str, answer: str, *, offset: int = 0) -> QueryRun:
    return QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="thread-history",
        api_key_id=uuid.uuid4(),
        question=question,
        answer=answer,
        created_at=datetime.now(UTC) + timedelta(seconds=offset),
    )


def test_bounded_history_keeps_recent_turns_in_chronological_order() -> None:
    newest = _run("the titles", "Which titles?", offset=2)
    older = _run("what documents do you have", "Which documents?", offset=1)

    history = bounded_history([newest, older], char_limit=1_000)

    assert history == [
        {"question": older.question, "answer": older.answer},
        {"question": newest.question, "answer": newest.answer},
    ]


def test_bounded_history_truncates_one_oversized_recent_answer() -> None:
    run = _run("question", "a" * 100)

    assert bounded_history([run], char_limit=20) == [{"question": "question", "answer": "a" * 12}]


def test_missing_inline_citations_are_appended_once() -> None:
    assert append_inline_citations("Grounded claim [E1]", ["E1", "E2"]) == (
        "Grounded claim [E1] [E2]"
    )


class _Scalars:
    def __init__(self, runs: list[QueryRun]) -> None:
        self.runs = runs

    def all(self) -> list[QueryRun]:
        return self.runs


class _Session:
    def __init__(self, runs: list[QueryRun]) -> None:
        self.runs = runs
        self.statement: Any = None

    async def scalars(self, statement: Any) -> _Scalars:
        self.statement = statement
        return _Scalars(self.runs)

    def add(self, value: Any) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()

    async def flush(self) -> None:
        return None


class _LLM:
    def __init__(self) -> None:
        self.user = ""
        self.system = ""

    async def structured(
        self,
        schema: object,
        system: str,
        user: str,
        *,
        thinking: bool,
    ) -> RouteDecision:
        del schema, thinking
        self.system = system
        self.user = user
        return RouteDecision(
            route=RunRoute.clarify,
            reason="confirmation needed",
            standalone_question="List every paper title in the catalog",
            clarification_question="Do you want every title?",
        )


class _Observability:
    def span(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        return nullcontext()


async def test_runner_routes_with_scoped_completed_history() -> None:
    api_key_id = uuid.uuid4()
    prior = _run("what documents do you have", "Which documents?")
    prior.api_key_id = api_key_id
    current = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="thread-history",
        api_key_id=api_key_id,
        question="the titles",
        created_at=datetime.now(UTC),
    )
    llm = _LLM()
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )
    session = _Session([prior])

    result = await runner.run(
        session,  # pyright: ignore[reportArgumentType]
        ChatRequest(message="the titles", thread_id=current.thread_id),
        current,
    )

    assert result["route"] == RunRoute.clarify
    route_input = json.loads(llm.user)
    assert route_input == {
        "history": [{"question": "what documents do you have", "answer": "Which documents?"}],
        "current_request": "the titles",
    }
    assert "unfiltered or filtered lists" in llm.system
    assert "exact or quoted phrases" in llm.system
    statement = str(session.statement)
    assert "query_runs.api_key_id" in statement
    assert "query_runs.error IS NULL" in statement


class _SQLLLM:
    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    async def structured(
        self,
        schema: object,
        system: str,
        user: str,
        *,
        thinking: bool,
    ) -> RouteDecision | SQLPlan | SynthesisResult:
        del user, thinking
        self.system_prompts.append(system)
        if schema is RouteDecision:
            return RouteDecision(
                route=RunRoute.sql,
                reason="catalog inventory",
                standalone_question="List every paper title in the catalog",
            )
        if schema is SQLPlan:
            return SQLPlan(
                sql="SELECT document_id, title FROM agent.paper_catalog_v1",
                explanation="List titles",
            )
        if schema is SynthesisResult:
            return SynthesisResult(
                answer="The catalog contains One-page verification fixture.",
                citation_ids=[],
            )
        raise AssertionError(f"unexpected schema: {schema}")


class _SQLAdapter:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def execute(self, query: object) -> SQLResult:
        del query
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise ProgrammingError("SELECT missing", {}, Exception("undefined column"))
        return SQLResult(
            columns=["document_id", "title"],
            rows=[{"document_id": str(uuid.uuid4()), "title": "One-page verification fixture"}],
            explain_cost=1.0,
            payload_bytes=100,
            result_hash="result-hash",
        )


async def test_sql_synthesis_gets_resolvable_inline_audit_citation() -> None:
    api_key_id = uuid.uuid4()
    current = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="thread-sql",
        api_key_id=api_key_id,
        question="paper titles you have access to",
        created_at=datetime.now(UTC),
    )
    llm = _SQLLLM()
    sql_adapter = _SQLAdapter(fail_first=True)
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        sql_adapter,  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"].endswith("[E1]")
    assert result["insufficient"] is False
    assert len(result["citations"]) == 1
    assert result["citations"][0]["source_type"] == "sql"
    assert result.get("sql_audit_id") is not None
    assert result["citations"][0]["sql_audit_id"] == result.get("sql_audit_id")
    assert sql_adapter.calls == 2
    assert any(
        "agent.ingestion_summary_v1(status, document_count" in value for value in llm.system_prompts
    )
