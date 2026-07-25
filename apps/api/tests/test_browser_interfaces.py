from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

import vectorless_rag.api as api_module
from vectorless_rag.api import (
    app,
    decode_thread_cursor,
    encode_thread_cursor,
    identity,
    normalize_thread_title,
    thread_history,
    threads,
)
from vectorless_rag.auth import Principal
from vectorless_rag.config import Settings
from vectorless_rag.models import QueryRun, RunFailureCode, RunRoute
from vectorless_rag.schemas import ChatRequest


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self._rows)


class _ScalarRows:
    def __init__(self, rows: list[QueryRun]) -> None:
        self._rows = rows

    def all(self) -> list[QueryRun]:
        return self._rows


class _Session:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statement: Any = None

    async def execute(self, statement: Any) -> _Result:
        self.statement = statement
        return _Result(self.rows)


class _HistorySession:
    def __init__(self, rows: list[QueryRun]) -> None:
        self.rows = rows
        self.statement: Any = None

    async def scalars(self, statement: Any) -> _ScalarRows:
        self.statement = statement
        return _ScalarRows(self.rows)


class _FailingChatSession:
    def __init__(self) -> None:
        self.run: QueryRun | None = None
        self.commits = 0

    def add(self, value: QueryRun) -> None:
        self.run = value

    async def flush(self) -> None:
        assert self.run is not None
        self.run.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1


class _FailingRunner:
    async def run(self, session: object, payload: object, run: QueryRun) -> None:
        del session, payload
        run.route = RunRoute.sql
        raise RuntimeError("generated SQL failed")


def _failing_runner(runtime: object) -> _FailingRunner:
    del runtime
    return _FailingRunner()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/admin/ingestion/{job_id}/retry",
        "/v1/admin/documents/{document_id}/retry",
    ],
)
def test_retry_ingestion_routes_require_admin_ingest_scope(path: str) -> None:
    route = next(
        route for route in app.routes if isinstance(route, APIRoute) and route.path == path
    )
    principal = next(
        dependency for dependency in route.dependant.dependencies if dependency.name == "principal"
    )

    assert route.methods == {"POST"}
    assert principal.own_oauth_scopes == ["admin:ingest"]


async def test_identity_returns_only_name_and_sorted_scopes() -> None:
    principal = Principal(uuid.uuid4(), "browser-test", frozenset({"chat", "documents:read"}))

    response = await identity(principal)

    assert response.model_dump() == {
        "name": "browser-test",
        "scopes": ["chat", "documents:read"],
    }
    assert "key" not in response.model_dump_json()


async def test_synchronous_chat_commits_failed_run_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "_runner", _failing_runner)
    runtime = SimpleNamespace(settings=Settings())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    principal = Principal(uuid.uuid4(), "test", frozenset({"chat"}))
    session = _FailingChatSession()

    with pytest.raises(HTTPException) as raised:
        await api_module._execute_chat(  # pyright: ignore[reportPrivateUsage]
            request,  # pyright: ignore[reportArgumentType]
            ChatRequest(message="how many docs deal with rag"),
            principal,
            session,  # pyright: ignore[reportArgumentType]
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == "The API could not complete this run."
    assert session.commits == 2
    assert session.run is not None
    assert session.run.route == RunRoute.sql
    assert session.run.error == "The API could not complete this run."
    assert session.run.failure_code == RunFailureCode.request_failed
    assert session.run.insufficient is True
    assert session.run.latency_ms is not None


async def test_synchronous_chat_preserves_unconfigured_provider_status_and_detail() -> None:
    runtime = SimpleNamespace(
        settings=Settings(release="private\nrelease"),
        llm=None,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    principal = Principal(uuid.uuid4(), "test", frozenset({"chat"}))
    session = _FailingChatSession()

    with pytest.raises(HTTPException) as raised:
        await api_module._execute_chat(  # pyright: ignore[reportPrivateUsage]
            request,  # pyright: ignore[reportArgumentType]
            ChatRequest(message="how many docs deal with rag"),
            principal,
            session,  # pyright: ignore[reportArgumentType]
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "DeepSeek is not configured"
    assert session.commits == 2
    assert session.run is not None
    assert session.run.error == "The API could not complete this run."
    assert session.run.failure_code == RunFailureCode.request_failed
    assert session.run.insufficient is True


async def test_thread_history_returns_running_without_a_failure_code() -> None:
    principal = Principal(uuid.uuid4(), "test", frozenset({"chat"}))
    run = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="thread-running",
        api_key_id=principal.api_key_id,
        question="What is still running?",
        citations=[],
        insufficient=False,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
    )
    session = _HistorySession([run])

    response = await thread_history(
        "thread-running",
        principal,
        session,  # pyright: ignore[reportArgumentType]
    )

    assert response.runs[0].status == "running"
    assert response.runs[0].failure_code is None
    assert response.runs[0].answer is None


async def test_threads_normalize_titles_order_and_emit_opaque_cursor() -> None:
    now = datetime(2026, 7, 20, 8, 30, tzinfo=UTC)
    session = _Session(
        [
            {
                "thread_id": "thread-z",
                "question": "  First\n question   with spacing  ",
                "run_count": 3,
                "last_activity_at": now,
            },
            {
                "thread_id": "thread-a",
                "question": "Second question",
                "run_count": 1,
                "last_activity_at": now,
            },
        ]
    )
    principal = Principal(uuid.uuid4(), "test", frozenset({"chat"}))

    page = await threads(principal, session, limit=1)  # pyright: ignore[reportArgumentType]

    assert [(item.thread_id, item.title, item.run_count) for item in page.items] == [
        ("thread-z", "First question with spacing", 3)
    ]
    assert page.next_cursor is not None
    assert "thread-z" not in page.next_cursor
    assert decode_thread_cursor(page.next_cursor) == (now, "thread-z")
    sql = str(session.statement)
    assert "query_runs.api_key_id" in sql
    assert "last_activity_at DESC" in sql
    assert "thread_id DESC" in sql


def test_thread_cursor_rejects_malformed_values_and_title_is_bounded() -> None:
    with pytest.raises(HTTPException) as exc_info:
        decode_thread_cursor("not-a-cursor")
    assert exc_info.value.status_code == 422
    assert len(normalize_thread_title("x" * 200)) == 120


def test_thread_cursor_round_trip() -> None:
    timestamp = datetime(2026, 7, 20, 12, tzinfo=UTC)
    assert decode_thread_cursor(encode_thread_cursor(timestamp, "abc")) == (timestamp, "abc")
