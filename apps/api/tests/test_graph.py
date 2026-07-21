from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import ProgrammingError

from vectorless_rag.config import Settings
from vectorless_rag.graph import (
    CATALOG_FALLBACK_SQL,
    GraphRunner,
    _normalize_regex_boundary_escapes,  # pyright: ignore[reportPrivateUsage]
    _strip_sql_comments,  # pyright: ignore[reportPrivateUsage]
    bounded_history,
    format_inline_citations,
    remove_inline_citations,
)
from vectorless_rag.models import QueryRun, RunRoute, SqlAudit
from vectorless_rag.retrieval import RetrievalResult
from vectorless_rag.schemas import ChatRequest, Evidence, RouteDecision, SQLPlan, SynthesisResult
from vectorless_rag.sql_guard import SQLRejected, SQLResult, ValidatedSQL, validate_sql

FALLBACK_ANSWER = "I could not find sufficient evidence in the indexed corpus to answer safely."


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


def test_internal_evidence_labels_become_user_facing_citation_numbers() -> None:
    assert format_inline_citations("Grounded claim [E1]", ["E1", "E2"]) == (
        "Grounded claim [1] [2]"
    )
    assert format_inline_citations("Grounded claim [1]", ["E1"]) == "Grounded claim [1]"


def test_sql_evidence_labels_are_removed_when_the_audit_is_not_user_visible() -> None:
    assert remove_inline_citations("Catalog result [E1]", ["E1"]) == "Catalog result"
    assert remove_inline_citations("Catalog result [1]", ["E1"]) == "Catalog result"


class _Scalars:
    def __init__(self, runs: list[QueryRun]) -> None:
        self.runs = runs

    def all(self) -> list[QueryRun]:
        return self.runs


class _Session:
    def __init__(self, runs: list[QueryRun]) -> None:
        self.runs = runs
        self.statement: Any = None
        self.added: list[Any] = []

    async def scalars(self, statement: Any) -> _Scalars:
        self.statement = statement
        return _Scalars(self.runs)

    def add(self, value: Any) -> None:
        self.added.append(value)
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
    def __init__(self) -> None:
        self.scores: list[tuple[str, str, float, str | None]] = []

    def span(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        return nullcontext()

    def score(
        self,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        self.scores.append((trace_id, name, value, comment))


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
    assert "are sql requests over titles, abstracts, and topics" in llm.system
    assert result["insufficient"] is False
    statement = str(session.statement)
    assert "query_runs.api_key_id" in statement
    assert "query_runs.error IS NULL" in statement


class _SQLLLM:
    def __init__(
        self,
        *,
        route: RunRoute = RunRoute.sql,
        synthesis_answer: str = "The catalog contains One-page verification fixture.",
        citation_ids: list[str] | None = None,
        sql: str = "SELECT document_id, title FROM agent.paper_catalog_v1",
        synthesis_insufficient: bool = False,
    ) -> None:
        self.route = route
        self.synthesis_answer = synthesis_answer
        self.citation_ids = citation_ids or []
        self.sql = sql
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []
        self.schemas: list[object] = []
        self.synthesis_insufficient = synthesis_insufficient

    async def structured(
        self,
        schema: object,
        system: str,
        user: str,
        *,
        thinking: bool,
    ) -> RouteDecision | SQLPlan | SynthesisResult:
        del thinking
        self.system_prompts.append(system)
        self.user_prompts.append(user)
        self.schemas.append(schema)
        if schema is RouteDecision:
            return RouteDecision(
                route=self.route,
                reason="catalog inventory",
                standalone_question="List every paper title in the catalog",
            )
        if schema is SQLPlan:
            return SQLPlan(
                sql=self.sql,
                explanation="List titles",
            )
        if schema is SynthesisResult:
            return SynthesisResult(
                answer=self.synthesis_answer,
                citation_ids=self.citation_ids,
                insufficient=self.synthesis_insufficient,
            )
        raise AssertionError(f"unexpected schema: {schema}")


class _SQLAdapter:
    def __init__(
        self,
        *,
        fail_first: bool = False,
        empty: bool = False,
        failure: Exception | None = None,
    ) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.empty = empty
        self.failure = failure
        self.queries: list[ValidatedSQL] = []

    async def execute(self, query: ValidatedSQL) -> SQLResult:
        self.queries.append(query)
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        if self.fail_first and self.calls == 1:
            raise ProgrammingError("SELECT missing", {}, Exception("undefined column"))
        return SQLResult(
            columns=["document_id", "title"],
            rows=(
                []
                if self.empty
                else [
                    {
                        "document_id": str(uuid.uuid4()),
                        "title": "One-page verification fixture",
                    }
                ]
            ),
            explain_cost=1.0,
            payload_bytes=100,
            result_hash="result-hash",
        )


class _Retriever:
    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.restrict_ids: list[uuid.UUID] | None = None

    async def retrieve(
        self,
        session: object,
        question: str,
        constraints: object,
        restrict_ids: list[uuid.UUID] | None = None,
    ) -> RetrievalResult:
        del session, question, constraints
        self.restrict_ids = restrict_ids
        return self.result


def _current(question: str, thread_id: str) -> QueryRun:
    return QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id=thread_id,
        api_key_id=uuid.uuid4(),
        question=question,
        created_at=datetime.now(UTC),
    )


async def test_sql_synthesis_keeps_audit_provenance_internal() -> None:
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

    assert result["answer"] == "The catalog contains One-page verification fixture."
    assert current.route == RunRoute.sql
    assert result["insufficient"] is False
    assert result["citations"] == []
    assert result.get("sql_audit_id") is not None
    assert sql_adapter.calls == 2
    assert any(
        "agent.ingestion_summary_v1(status text, document_count bigint" in value
        for value in llm.system_prompts
    )
    assert any("authors text[]" in value for value in llm.system_prompts)
    assert any("array_to_string(column, ' ')" in value for value in llm.system_prompts)
    assert any("do not split abstract text into words" in value for value in llm.system_prompts)
    assert any("do not assemble display text" in value for value in llm.system_prompts)
    assert any("PostgreSQL regex word boundaries" in value for value in llm.system_prompts)
    assert any("authors and topics are text[]" in value for value in llm.system_prompts)
    assert any(
        "When the SQL filtered or counted rows by text or pattern matching" in value
        for value in llm.system_prompts
    )
    sql_provenance_lines = [
        line
        for prompt in llm.user_prompts
        for line in prompt.splitlines()
        if line.startswith("SQL used (catalog metadata only):")
    ]
    assert len(sql_provenance_lines) == 1
    assert "LIMIT 200" in sql_provenance_lines[0]


async def test_empty_sql_result_is_sufficient_without_a_user_citation() -> None:
    current = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="thread-empty-sql",
        api_key_id=uuid.uuid4(),
        question="what are they",
        created_at=datetime.now(UTC),
    )
    llm = _SQLLLM(synthesis_insufficient=True)
    observability = _Observability()
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        _SQLAdapter(empty=True),  # pyright: ignore[reportArgumentType]
        observability,  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == "No matching catalog records were found."
    assert result["insufficient"] is False
    assert result["citations"] == []
    assert result.get("sql_audit_id") is not None
    assert any("including a zero count or empty row set" in value for value in llm.system_prompts)
    assert all(score[1] != "sql-comment-stripped" for score in observability.scores)


@pytest.mark.parametrize(
    ("generated_sql", "comment_marker"),
    [
        (
            "-- pick catalog\nSELECT document_id, title FROM agent.paper_catalog_v1",
            "--",
        ),
        (
            "/* overview */ SELECT document_id, title FROM agent.paper_catalog_v1",
            "/*",
        ),
    ],
    ids=["line-comment", "block-comment"],
)
async def test_sql_route_strips_model_comments_before_validation(
    generated_sql: str, comment_marker: str
) -> None:
    current = _current("List the papers", f"thread-sql-{comment_marker}")
    llm = _SQLLLM(sql=generated_sql)
    adapter = _SQLAdapter()
    session = _Session([])
    observability = _Observability()
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        adapter,  # pyright: ignore[reportArgumentType]
        observability,  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        session,  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == "The catalog contains One-page verification fixture."
    assert llm.schemas.count(SQLPlan) == 1
    assert adapter.calls == 1
    audits = [item for item in session.added if isinstance(item, SqlAudit)]
    assert len(audits) == 1
    assert audits[0].generated_sql == generated_sql
    assert audits[0].normalized_sql is not None
    assert comment_marker not in audits[0].normalized_sql
    assert "--" not in audits[0].normalized_sql
    assert "/*" not in audits[0].normalized_sql
    assert observability.scores == [(current.trace_id, "sql-comment-stripped", 1.0, None)]


async def test_unparseable_sql_still_runs_repair_and_raises_terminal_rejection() -> None:
    current = _current("List the papers", "thread-sql-unparseable")
    llm = _SQLLLM(sql="SELECT FROM WHERE")
    adapter = _SQLAdapter()
    session = _Session([])
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        adapter,  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    with pytest.raises(SQLRejected, match="invalid PostgreSQL syntax"):
        await runner.run(
            session,  # pyright: ignore[reportArgumentType]
            ChatRequest(message=current.question, thread_id=current.thread_id),
            current,
        )

    assert llm.schemas.count(SQLPlan) == 2
    assert adapter.calls == 0
    audits = [item for item in session.added if isinstance(item, SqlAudit)]
    assert len(audits) == 2
    assert all(audit.generated_sql == "SELECT FROM WHERE" for audit in audits)
    assert all("invalid PostgreSQL syntax" in (audit.rejection_reason or "") for audit in audits)


async def test_unterminated_string_sql_runs_repair_and_raises_terminal_rejection() -> None:
    sql = "SELECT title FROM agent.paper_catalog_v1 WHERE title = 'a"
    current = _current("List the papers", "thread-sql-unterminated-string")
    llm = _SQLLLM(sql=sql)
    adapter = _SQLAdapter()
    session = _Session([])
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        adapter,  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    with pytest.raises(SQLRejected, match="invalid PostgreSQL syntax"):
        await runner.run(
            session,  # pyright: ignore[reportArgumentType]
            ChatRequest(message=current.question, thread_id=current.thread_id),
            current,
        )

    assert llm.schemas.count(SQLPlan) == 2
    assert adapter.calls == 0
    audits = [item for item in session.added if isinstance(item, SqlAudit)]
    assert len(audits) == 2
    assert all(audit.generated_sql == sql for audit in audits)
    assert all("invalid PostgreSQL syntax" in (audit.rejection_reason or "") for audit in audits)


async def test_run_sql_recursion_error_degrades_to_rejection_after_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_recursion(*args: object, **kwargs: object) -> ValidatedSQL:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("vectorless_rag.graph.validate_sql", _raise_recursion)
    current = _current("List deeply nested catalog rows", "thread-sql-recursion")
    llm = _SQLLLM(sql="SELECT title FROM agent.paper_catalog_v1")
    adapter = _SQLAdapter()
    session = _Session([])
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        adapter,  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    with pytest.raises(SQLRejected, match="too deeply nested"):
        await runner._run_sql(  # pyright: ignore[reportPrivateUsage]
            session,  # pyright: ignore[reportArgumentType]
            current,
            current.question,
        )

    assert llm.schemas.count(SQLPlan) == 2
    assert adapter.calls == 0
    audits = [item for item in session.added if isinstance(item, SqlAudit)]
    assert len(audits) == 2
    assert all(audit.rejection_reason for audit in audits)


@pytest.mark.parametrize(
    "sql",
    [
        "-- pick catalog\nSELECT document_id FROM agent.paper_catalog_v1",
        "/* overview */ SELECT document_id FROM agent.paper_catalog_v1",
    ],
    ids=["line-comment", "block-comment"],
)
def test_strip_sql_comments_removes_comments_from_single_statement(sql: str) -> None:
    stripped = _strip_sql_comments(sql)

    assert stripped == "SELECT document_id FROM agent.paper_catalog_v1"
    assert "--" not in stripped
    assert "/*" not in stripped


def test_strip_sql_comments_returns_multiple_statements_unchanged() -> None:
    sql = "SELECT 1; SELECT 2"

    assert _strip_sql_comments(sql) == sql


def test_strip_sql_comments_returns_unparseable_sql_unchanged() -> None:
    sql = "SELECT FROM WHERE"

    assert _strip_sql_comments(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT title FROM agent.paper_catalog_v1 WHERE title = 'a",
        "SELECT title FROM agent.paper_catalog_v1 /* oops",
    ],
    ids=["unterminated-string", "unterminated-block-comment"],
)
def test_strip_sql_comments_returns_token_errors_unchanged(sql: str) -> None:
    assert _strip_sql_comments(sql) == sql


def test_normalize_regex_boundary_escapes_collapses_doubled_word_boundaries() -> None:
    sql = r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\\mdistillation\\M'"

    out = _normalize_regex_boundary_escapes(sql)

    assert r"\\m" not in out
    assert r"\m" in out
    assert r"\\M" not in out
    assert r"\M" in out


def test_normalize_regex_boundary_escapes_is_idempotent() -> None:
    sql = r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\\mdistillation\\M'"

    once = _normalize_regex_boundary_escapes(sql)
    twice = _normalize_regex_boundary_escapes(once)

    assert once == twice
    assert r"\\m" not in twice


def test_normalize_regex_boundary_escapes_leaves_single_backslash_unchanged() -> None:
    sql = r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\mdistillation\M'"

    assert _normalize_regex_boundary_escapes(sql) == sql


def test_normalize_regex_boundary_escapes_leaves_boundary_free_sql_unchanged() -> None:
    sql = "SELECT document_id, title FROM agent.paper_catalog_v1"

    assert _normalize_regex_boundary_escapes(sql) == sql


def test_normalize_regex_boundary_escapes_ignores_non_regex_string_literals() -> None:
    sql = r"SELECT title FROM agent.paper_catalog_v1 WHERE title = '\\model'"

    out = _normalize_regex_boundary_escapes(sql)

    assert out == sql
    assert r"\\model" in out


def test_normalize_regex_boundary_escapes_scopes_collapse_to_regex_patterns() -> None:
    sql = (
        r"SELECT COUNT(*) FROM agent.paper_catalog_v1 "
        r"WHERE title ~* '\\mrag\\M' AND arxiv_id = '\\model'"
    )

    out = _normalize_regex_boundary_escapes(sql)

    assert r"\mrag\M" in out
    assert r"\\mrag\\M" not in out
    assert r"\\model" in out


def test_normalize_regex_boundary_escapes_leaves_class_escapes_untouched() -> None:
    sql = r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\\d\\s\\w'"

    assert _normalize_regex_boundary_escapes(sql) == sql


def test_normalize_regex_boundary_escapes_returns_unparseable_sql_unchanged() -> None:
    sql = "SELECT FROM WHERE"

    assert _normalize_regex_boundary_escapes(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~ '\\mrag\\M'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\\mrag\\M'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE regexp_like(title, '\\mrag\\M')",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE regexp_ilike(title, '\\mrag\\M')",
    ],
    ids=["tilde-match", "tilde-imatch", "regexp_like", "regexp_ilike"],
)
def test_normalize_regex_boundary_escapes_covers_all_match_forms(sql: str) -> None:
    out = _normalize_regex_boundary_escapes(sql)

    assert r"\mrag\M" in out
    assert r"\\mrag\\M" not in out


def test_normalize_regex_boundary_escapes_enables_guarded_short_term_match() -> None:
    doubled = r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\\yrag\\y'"

    normalized = _normalize_regex_boundary_escapes(doubled)

    assert r"\yrag\y" in normalized
    assert r"\\yrag\\y" not in normalized
    validated = validate_sql(normalized)
    assert r"\yrag\y" in validated.normalized


async def test_sql_route_normalizes_doubled_regex_boundaries_before_execution() -> None:
    doubled_sql = r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\\mdistillation\\M'"
    current = _current("how many papers deal with distillation", "thread-sql-regex")
    llm = _SQLLLM(sql=doubled_sql)
    adapter = _SQLAdapter()
    session = _Session([])
    observability = _Observability()
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        adapter,  # pyright: ignore[reportArgumentType]
        observability,  # pyright: ignore[reportArgumentType]
    )

    await runner.run(
        session,  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert adapter.calls == 1
    executed = adapter.queries[0].normalized
    assert r"\mdistillation\M" in executed
    assert r"\\mdistillation\\M" not in executed
    audits = [item for item in session.added if isinstance(item, SqlAudit)]
    assert len(audits) == 1
    assert audits[0].generated_sql == doubled_sql
    assert r"\\mdistillation\\M" in audits[0].generated_sql
    assert audits[0].normalized_sql is not None
    assert r"\mdistillation\M" in audits[0].normalized_sql
    assert r"\\mdistillation\\M" not in audits[0].normalized_sql
    assert (current.trace_id, "sql-regex-normalized", 1.0, None) in observability.scores
    assert all(score[1] != "sql-comment-stripped" for score in observability.scores)


async def test_hybrid_non_empty_sql_rows_bypass_document_insufficiency() -> None:
    current = _current("Which catalog paper answers this?", "thread-hybrid-sql")
    llm = _SQLLLM(
        route=RunRoute.hybrid,
        synthesis_answer="The catalog row provides the answer [E1].",
        citation_ids=["E1"],
        synthesis_insufficient=True,
    )
    retriever = _Retriever(RetrievalResult([], [], [], False, 2))
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        retriever,  # pyright: ignore[reportArgumentType]
        _SQLAdapter(),  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == "The catalog row provides the answer [E1]."
    assert result["answer"] != FALLBACK_ANSWER
    assert result["insufficient"] is True
    assert result["citations"][0]["source_type"] == "sql"
    assert result.get("sql_audit_id") is not None
    assert result["citations"][0]["sql_audit_id"] == result.get("sql_audit_id")
    assert retriever.restrict_ids is not None and len(retriever.restrict_ids) == 1
    assert any("SQL used (catalog metadata only):" in value for value in llm.user_prompts)


async def test_hybrid_empty_sql_rows_preserve_document_insufficiency_fallback() -> None:
    current = _current("Which catalog paper answers this?", "thread-hybrid-empty")
    llm = _SQLLLM(route=RunRoute.hybrid)
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        _Retriever(RetrievalResult([], [], [], False, 2)),  # pyright: ignore[reportArgumentType]
        _SQLAdapter(empty=True),  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == FALLBACK_ANSWER
    assert result["insufficient"] is True
    assert SynthesisResult not in llm.schemas


async def test_documents_empty_retrieval_uses_guarded_catalog_fallback() -> None:
    current = _current("What indexed material is available?", "thread-doc-catalog")
    llm = _SQLLLM(
        route=RunRoute.documents,
        synthesis_answer="The catalog includes the fixture [E1].",
        citation_ids=["E1"],
    )
    adapter = _SQLAdapter()
    session = _Session([])
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        _Retriever(RetrievalResult([], [], [], False, 0)),  # pyright: ignore[reportArgumentType]
        adapter,  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        session,  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == "The catalog includes the fixture [1]."
    assert result["answer"] != FALLBACK_ANSWER
    assert result["insufficient"] is False
    assert result["citations"][0]["source_type"] == "sql"
    audits = [item for item in session.added if isinstance(item, SqlAudit)]
    assert len(audits) == 1
    assert audits[0].generated_sql == CATALOG_FALLBACK_SQL
    assert audits[0].allowed is True
    assert audits[0].normalized_sql is not None
    assert "LIMIT 200" in audits[0].normalized_sql
    assert SQLPlan not in llm.schemas
    assert any("One-page verification fixture" in value for value in llm.user_prompts)
    assert len(adapter.queries) == 1
    assert "LIMIT 200" in adapter.queries[0].normalized


@pytest.mark.parametrize(
    "failure",
    [
        SQLRejected("configuration drift"),
        ProgrammingError("stmt", None, Exception("boom")),
    ],
    ids=["sql-rejected", "programming-error"],
)
async def test_documents_catalog_fallback_rejection_preserves_refusal(
    failure: Exception,
) -> None:
    current = _current("What indexed material is available?", "thread-doc-rejected")
    llm = _SQLLLM(route=RunRoute.documents)
    session = _Session([])
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        _Retriever(RetrievalResult([], [], [], False, 0)),  # pyright: ignore[reportArgumentType]
        _SQLAdapter(failure=failure),  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        session,  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == FALLBACK_ANSWER
    assert result["insufficient"] is True
    audits = [item for item in session.added if isinstance(item, SqlAudit)]
    assert len(audits) == 1
    assert audits[0].allowed is False
    assert audits[0].rejection_reason
    assert SynthesisResult not in llm.schemas


async def test_documents_synthesis_promotes_known_marker_not_declared_in_citation_ids() -> None:
    current = _current("Compare the papers", "thread-doc-marker-mismatch")
    evidence = [
        Evidence(
            source_type="document",
            source_id=str(uuid.uuid4()),
            title=f"Paper {index}",
            node_id=f"node-{index}",
            page_start=index,
            page_end=index,
            content=f"Evidence {index}",
            retrieval_reason="Relevant passage",
        )
        for index in range(1, 3)
    ]
    runner = GraphRunner(
        Settings(),
        _SQLLLM(
            route=RunRoute.documents,
            synthesis_answer="The second paper supports this claim [E2].",
            citation_ids=["E1"],
        ),  # pyright: ignore[reportArgumentType]
        _Retriever(
            RetrievalResult(
                evidence,
                [uuid.UUID(item.source_id) for item in evidence],
                [uuid.UUID(item.source_id) for item in evidence],
                True,
                1,
            )
        ),  # pyright: ignore[reportArgumentType]
        _SQLAdapter(),  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == "The second paper supports this claim [2]. [1]"
    assert [citation["source_id"] for citation in result["citations"]] == [
        item.source_id for item in evidence
    ]
    assert result["insufficient"] is False


async def test_documents_synthesis_rejects_unknown_inline_citation_marker() -> None:
    current = _current("Compare the papers", "thread-doc-unknown-marker")
    evidence = [
        Evidence(
            source_type="document",
            source_id=str(uuid.uuid4()),
            title=f"Paper {index}",
            node_id=f"node-{index}",
            page_start=index,
            page_end=index,
            content=f"Evidence {index}",
            retrieval_reason="Relevant passage",
        )
        for index in range(1, 3)
    ]
    runner = GraphRunner(
        Settings(),
        _SQLLLM(
            route=RunRoute.documents,
            synthesis_answer="The fifth paper supports this claim [E5].",
            citation_ids=["E1"],
        ),  # pyright: ignore[reportArgumentType]
        _Retriever(
            RetrievalResult(
                evidence,
                [uuid.UUID(item.source_id) for item in evidence],
                [uuid.UUID(item.source_id) for item in evidence],
                True,
                1,
            )
        ),  # pyright: ignore[reportArgumentType]
        _SQLAdapter(),  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == "I could not produce citations that resolve to retrieved evidence."
    assert result["citations"] == []
    assert result["insufficient"] is True


async def test_sql_synthesis_rejects_unknown_inline_citation_marker() -> None:
    current = _current("List the papers", "thread-sql-unknown-marker")
    runner = GraphRunner(
        Settings(),
        _SQLLLM(synthesis_answer="The catalog contains a paper [E9]."),  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        _SQLAdapter(),  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == "I could not produce citations that resolve to retrieved evidence."
    assert result["citations"] == []
    assert result["insufficient"] is True


async def test_documents_non_empty_insufficient_evidence_preserves_refusal() -> None:
    current = _current("Explain the paper", "thread-doc-insufficient")
    llm = _SQLLLM(route=RunRoute.documents)
    document_id = uuid.uuid4()
    evidence = Evidence(
        source_type="document",
        source_id=str(document_id),
        title="Partial paper",
        node_id="node-1",
        page_start=1,
        page_end=1,
        content="Partial evidence",
        retrieval_reason="Possibly relevant",
    )
    adapter = _SQLAdapter()
    runner = GraphRunner(
        Settings(),
        llm,  # pyright: ignore[reportArgumentType]
        _Retriever(RetrievalResult([evidence], [document_id], [document_id], False, 2)),  # pyright: ignore[reportArgumentType]
        adapter,  # pyright: ignore[reportArgumentType]
        _Observability(),  # pyright: ignore[reportArgumentType]
    )

    result = await runner.run(
        _Session([]),  # pyright: ignore[reportArgumentType]
        ChatRequest(message=current.question, thread_id=current.thread_id),
        current,
    )

    assert result["answer"] == FALLBACK_ANSWER
    assert result["insufficient"] is True
    assert adapter.calls == 0
    assert SynthesisResult not in llm.schemas
