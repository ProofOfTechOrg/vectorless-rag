from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from vectorless_rag.api import (
    app,
    decode_thread_cursor,
    encode_thread_cursor,
    identity,
    normalize_thread_title,
    threads,
)
from vectorless_rag.auth import Principal


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


class _Session:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statement: Any = None

    async def execute(self, statement: Any) -> _Result:
        self.statement = statement
        return _Result(self.rows)


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
