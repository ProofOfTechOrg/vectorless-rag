from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import openai
from pydantic import SecretStr
from pytest import MonkeyPatch
from sqlalchemy.dialects import postgresql

from vectorless_rag.config import Settings
from vectorless_rag.db import set_ingestion_provider_admission
from vectorless_rag.models import (
    Document,
    DocumentStatus,
    JobStatus,
    ProviderAvailability,
    ProviderState,
)
from vectorless_rag.provider import (
    STATUS_CACHE_WINDOW,
    ProviderGate,
    classify_provider_failure,
    provider_configuration_hash,
)


def test_http_200_response_validation_failure_is_retryable() -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(200, request=request)
    error = openai.APIResponseValidationError(response=response, body={"invalid": True})

    failure = classify_provider_failure(error)

    assert failure.code == "upstream_invalid_response"
    assert failure.retryable
    assert failure.http_status == 200
    assert not failure.blocks_provider


class _Rows:
    def __init__(self, rows: Sequence[object]) -> None:
        self.rows = list(rows)

    def all(self) -> list[object]:
        return self.rows


class _ProviderSession:
    def __init__(self, state: ProviderState | None) -> None:
        self.state = state
        self.added: list[object] = []

    async def get(self, model: object, identifier: object) -> ProviderState | None:
        del model, identifier
        return self.state

    def add(self, value: object) -> None:
        self.added.append(value)
        if isinstance(value, ProviderState):
            self.state = value

    async def flush(self) -> None:
        return None


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("private provider body")
        self.status_code = status_code


class _ReturningResult:
    def __init__(self, document_ids: Sequence[uuid.UUID]) -> None:
        self.document_ids = document_ids

    def scalars(self) -> _Rows:
        return _Rows(self.document_ids)


class _AdmissionSession:
    def __init__(
        self,
        document_ids: Sequence[uuid.UUID],
        documents: Sequence[Document],
    ) -> None:
        self.document_ids = document_ids
        self.documents = documents
        self.statement: object | None = None
        self.scalar_calls = 0

    async def execute(self, statement: object) -> _ReturningResult:
        self.statement = statement
        return _ReturningResult(self.document_ids)

    async def scalars(self, statement: object) -> _Rows:
        del statement
        self.scalar_calls += 1
        return _Rows(self.documents)


def _state(
    settings: Settings,
    *,
    checked_at: datetime,
    availability: ProviderAvailability,
    reason: str | None,
    last_available_at: datetime | None = None,
) -> ProviderState:
    return ProviderState(
        provider="deepseek",
        availability=availability,
        reason=reason,
        checked_at=checked_at,
        configuration_hash=provider_configuration_hash(settings),
        last_available_at=last_available_at,
    )


async def test_recent_hard_provider_failure_does_not_probe(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings(deepseek_api_key=SecretStr("test-key"))
    checked_at = datetime.now(UTC)
    state = _state(
        settings,
        checked_at=checked_at,
        availability=ProviderAvailability.unavailable,
        reason="insufficient_credit",
    )
    gate = ProviderGate(settings)
    gate.observe_completion_failure(_StatusError(402))

    async def unexpected_probe() -> bool:
        raise AssertionError("a recent hard failure must stay cached")

    monkeypatch.setattr(gate, "_balance_available", unexpected_probe)

    observation = await gate.status(cast(Any, _ProviderSession(state)), refresh=False)

    assert observation.availability == ProviderAvailability.unavailable
    assert observation.reason == "insufficient_credit"
    assert observation.checked_at == checked_at


async def test_hard_provider_failure_is_shared_across_worker_gates(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings(deepseek_api_key=SecretStr("test-key"))
    state = _state(
        settings,
        checked_at=datetime.now(UTC),
        availability=ProviderAvailability.available,
        reason=None,
        last_available_at=datetime.now(UTC),
    )
    session = _ProviderSession(state)
    failing_worker = ProviderGate(settings)
    failing_worker.observe_completion_failure(_StatusError(401))

    first = await failing_worker.status(cast(Any, session), refresh=False)

    assert first.availability == ProviderAvailability.unavailable
    assert state.availability == ProviderAvailability.unavailable
    assert state.reason == "authentication_failed"

    other_worker = ProviderGate(settings)

    async def unexpected_probe() -> bool:
        raise AssertionError("another worker must honor the durable hard failure")

    monkeypatch.setattr(other_worker, "_balance_available", unexpected_probe)
    second = await other_worker.status(cast(Any, session), refresh=False)

    assert second.availability == ProviderAvailability.unavailable
    assert second.reason == "authentication_failed"


async def test_stale_hard_provider_failure_recovers_after_fresh_probe(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings(deepseek_api_key=SecretStr("test-key"))
    state = _state(
        settings,
        checked_at=datetime.now(UTC) - STATUS_CACHE_WINDOW - timedelta(seconds=1),
        availability=ProviderAvailability.unavailable,
        reason="insufficient_credit",
    )
    gate = ProviderGate(settings)
    gate.observe_completion_failure(_StatusError(402))
    probes = 0

    async def available() -> bool:
        nonlocal probes
        probes += 1
        return True

    monkeypatch.setattr(gate, "_balance_available", available)

    observation = await gate.status(cast(Any, _ProviderSession(state)), refresh=False)

    assert observation.availability == ProviderAvailability.available
    assert observation.stale is False
    assert gate.hard_failure is None
    assert probes == 1


async def test_fresh_recovery_from_another_worker_closes_the_local_hard_gate(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings(deepseek_api_key=SecretStr("test-key"))
    gate = ProviderGate(settings)
    gate.observe_completion_failure(_StatusError(401))
    state = _state(
        settings,
        checked_at=datetime.now(UTC) + timedelta(seconds=1),
        availability=ProviderAvailability.available,
        reason=None,
        last_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    async def unexpected_probe() -> bool:
        raise AssertionError("the newer durable observation is already authoritative")

    monkeypatch.setattr(gate, "_balance_available", unexpected_probe)
    observation = await gate.status(cast(Any, _ProviderSession(state)), refresh=False)

    assert observation.availability == ProviderAvailability.available
    assert gate.hard_failure is None


async def test_hard_failure_never_uses_a_stale_positive(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings(deepseek_api_key=SecretStr("test-key"))
    now = datetime.now(UTC)
    state = _state(
        settings,
        checked_at=now - STATUS_CACHE_WINDOW - timedelta(seconds=1),
        availability=ProviderAvailability.available,
        reason=None,
        last_available_at=now,
    )
    gate = ProviderGate(settings)
    gate.observe_completion_failure(_StatusError(401))

    async def unavailable() -> bool:
        raise TimeoutError

    monkeypatch.setattr(gate, "_balance_available", unavailable)

    observation = await gate.status(cast(Any, _ProviderSession(state)), refresh=False)

    assert observation.availability == ProviderAvailability.unavailable
    assert observation.reason == "authentication_failed"
    assert observation.stale is False
    assert gate.hard_failure is not None


async def test_provider_admission_uses_fresh_observation_and_returned_jobs_only() -> None:
    transitioned = Document(
        id=uuid.uuid4(),
        sha256="a" * 64,
        original_filename="transitioned.pdf",
        pdf_object_key="pdf/transitioned.pdf",
        status=DocumentStatus.waiting_provider,
        diagnostic="waiting",
    )
    deferred = Document(
        id=uuid.uuid4(),
        sha256="b" * 64,
        original_filename="deferred.pdf",
        pdf_object_key="pdf/deferred.pdf",
        status=DocumentStatus.deferred,
        diagnostic="cost cap",
    )
    checked_at = datetime.now(UTC)
    session = _AdmissionSession([transitioned.id], [transitioned])

    await set_ingestion_provider_admission(
        cast(Any, session),
        available=True,
        reason=None,
        checked_at=checked_at,
    )

    assert transitioned.status == DocumentStatus.queued
    assert transitioned.diagnostic is None
    assert deferred.status == DocumentStatus.deferred
    assert deferred.diagnostic == "cost cap"
    assert session.statement is not None
    compiled = cast(Any, session.statement).compile(dialect=postgresql.dialect())
    parameters = list(compiled.params.values())
    assert JobStatus.waiting_provider in parameters
    assert JobStatus.deferred not in parameters
    assert checked_at - STATUS_CACHE_WINDOW in parameters


async def test_provider_admission_does_not_scan_documents_without_transitions() -> None:
    session = _AdmissionSession([], [])

    await set_ingestion_provider_admission(
        cast(Any, session),
        available=True,
        reason=None,
        checked_at=datetime.now(UTC),
    )

    assert session.scalar_calls == 0
