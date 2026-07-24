from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import cast

import psycopg

from vectorless_rag.models import ModelCallOutcome, ModelCallStage

_SAFE_SCALAR = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")
_OWNER: ContextVar[UsageOwner | None] = ContextVar("model_call_owner", default=None)


class UsagePersistenceError(RuntimeError):
    failure_code = "usage_persistence_failed"
    diagnostic = "Model usage accounting could not be persisted."

    def __init__(self) -> None:
        super().__init__(self.diagnostic)


@dataclass(frozen=True)
class UsageOwner:
    trace_id: str
    query_run_id: uuid.UUID | None = None
    ingestion_attempt_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if (self.query_run_id is None) == (self.ingestion_attempt_id is None):
            raise ValueError("a usage owner must identify exactly one parent")
        if not _SAFE_SCALAR.fullmatch(self.trace_id) or len(self.trace_id) > 64:
            raise ValueError("trace_id is not a bounded opaque identifier")


@contextmanager
def usage_context(owner: UsageOwner) -> Generator[None]:
    token: Token[UsageOwner | None] = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(token)


def current_usage_owner() -> UsageOwner | None:
    return _OWNER.get()


def ingestion_owner_from_environment() -> UsageOwner | None:
    raw_attempt_id = os.getenv("VECTORLESS_INGESTION_ATTEMPT_ID")
    trace_id = os.getenv("VECTORLESS_INGESTION_TRACE_ID")
    if raw_attempt_id is None and trace_id is None:
        return None
    if raw_attempt_id is None or trace_id is None:
        raise UsagePersistenceError
    try:
        attempt_id = uuid.UUID(raw_attempt_id)
        return UsageOwner(trace_id=trace_id, ingestion_attempt_id=attempt_id)
    except ValueError as error:
        raise UsagePersistenceError from error


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _field(value: object, name: str) -> tuple[bool, object]:
    mapping = _as_mapping(value)
    if mapping is not None:
        return name in mapping, mapping.get(name)
    if value is None:
        return False, None
    return hasattr(value, name), getattr(value, name, None)


def _non_negative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _first_int(value: object, names: tuple[str, ...]) -> int | None:
    for name in names:
        _, raw = _field(value, name)
        parsed = _non_negative_int(raw)
        if parsed is not None:
            return parsed
    return None


def _nested_int(
    value: object, containers: tuple[str, ...], names: tuple[str, ...]
) -> tuple[bool, int | None]:
    for container_name in containers:
        present, container = _field(value, container_name)
        if not present or container is None:
            continue
        for name in names:
            nested_present, raw = _field(container, name)
            if nested_present:
                return True, _non_negative_int(raw)
    return False, None


def _direct_int(value: object, names: tuple[str, ...]) -> tuple[bool, int | None]:
    for name in names:
        present, raw = _field(value, name)
        if present:
            return True, _non_negative_int(raw)
    return False, None


@dataclass(frozen=True)
class TokenUsage:
    usage_available: bool
    cache_attribution_complete: bool
    input_tokens: int | None = None
    cache_hit_input_tokens: int | None = None
    cache_miss_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    unattributed_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def unavailable(cls) -> TokenUsage:
        return cls(usage_available=False, cache_attribution_complete=False)

    def langfuse_details(self) -> dict[str, int]:
        if not self.usage_available:
            return {}
        assert self.input_tokens is not None
        assert self.cache_hit_input_tokens is not None
        assert self.cache_miss_input_tokens is not None
        assert self.cache_write_input_tokens is not None
        assert self.unattributed_input_tokens is not None
        assert self.output_tokens is not None
        assert self.reasoning_output_tokens is not None
        assert self.total_tokens is not None
        return {
            "input": self.input_tokens,
            "cache_read_input_tokens": self.cache_hit_input_tokens,
            "cache_miss_input_tokens": self.cache_miss_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "unattributed_input_tokens": self.unattributed_input_tokens,
            "output": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total": self.total_tokens,
        }


def normalize_token_usage(usage: object) -> TokenUsage:
    if usage is None:
        return TokenUsage.unavailable()
    input_tokens = _first_int(usage, ("input_tokens", "prompt_tokens"))
    output_tokens = _first_int(usage, ("output_tokens", "completion_tokens"))
    if input_tokens is None or output_tokens is None:
        return TokenUsage.unavailable()

    hit_present, cache_hit = _nested_int(
        usage,
        ("input_token_details", "prompt_tokens_details"),
        ("cache_read", "cached_tokens"),
    )
    if not hit_present:
        hit_present, cache_hit = _direct_int(
            usage,
            (
                "cache_read_input_tokens",
                "prompt_cache_hit_tokens",
            ),
        )
    write_present, cache_write = _nested_int(
        usage,
        ("input_token_details", "prompt_tokens_details"),
        ("cache_creation", "cache_write"),
    )
    if not write_present:
        write_present, cache_write = _direct_int(
            usage,
            (
                "cache_creation_input_tokens",
                "cache_write_input_tokens",
            ),
        )
    miss_present, explicit_miss = _direct_int(
        usage,
        ("cache_miss_input_tokens", "prompt_cache_miss_tokens"),
    )

    cache_values_valid = (
        (not hit_present or cache_hit is not None)
        and (not write_present or cache_write is not None)
        and (not miss_present or explicit_miss is not None)
    )
    cache_details_present = hit_present or write_present or miss_present
    if not cache_values_valid or not cache_details_present:
        cache_hit = 0
        cache_miss = 0
        cache_write = 0
        unattributed = input_tokens
        cache_complete = input_tokens == 0
    else:
        cache_hit = cache_hit or 0
        if miss_present:
            cache_miss = explicit_miss or 0
            unattributed = input_tokens - cache_hit - cache_miss
        else:
            cache_miss = input_tokens - cache_hit
            unattributed = 0
        cache_write = cache_write or 0
        if (
            cache_hit > input_tokens
            or cache_miss < 0
            or unattributed < 0
            or cache_write > cache_miss
        ):
            cache_hit = 0
            cache_miss = 0
            cache_write = 0
            unattributed = input_tokens
            cache_complete = input_tokens == 0
        else:
            cache_complete = unattributed == 0

    _, reasoning = _nested_int(
        usage,
        ("output_token_details", "completion_tokens_details"),
        ("reasoning", "reasoning_tokens"),
    )
    if reasoning is None:
        _, reasoning = _direct_int(usage, ("reasoning_tokens",))
    reasoning = reasoning if reasoning is not None and reasoning <= output_tokens else 0

    return TokenUsage(
        usage_available=True,
        cache_attribution_complete=cache_complete,
        input_tokens=input_tokens,
        cache_hit_input_tokens=cache_hit,
        cache_miss_input_tokens=cache_miss,
        cache_write_input_tokens=cache_write,
        unattributed_input_tokens=unattributed,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
        total_tokens=input_tokens + output_tokens,
    )


@dataclass(frozen=True)
class StartedModelCall:
    id: uuid.UUID
    owner: UsageOwner
    stage: ModelCallStage
    model: str
    provider_attempt: int


_INSERT_CALL = """
INSERT INTO agent.model_calls (
    id, query_run_id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
    outcome, usage_available, cache_attribution_complete
)
SELECT
    %(id)s::uuid, %(query_run_id)s::uuid, %(ingestion_attempt_id)s::uuid, %(trace_id)s,
    %(stage)s::agent.model_call_stage, %(model)s, %(provider_attempt)s,
    'started'::agent.model_call_outcome, false, false
WHERE
    (
      %(query_run_id)s::uuid IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM agent.query_runs WHERE id = %(query_run_id)s::uuid
      )
    )
    OR
    (
      %(ingestion_attempt_id)s::uuid IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM agent.ingestion_attempts
        WHERE id = %(ingestion_attempt_id)s::uuid
          AND status = 'running'::agent.ingestion_attempt_status
      )
    )
RETURNING id
"""

_FINALIZE_CALL = """
UPDATE agent.model_calls
SET outcome = %(outcome)s::agent.model_call_outcome,
    finish_reason = %(finish_reason)s,
    input_tokens = %(input_tokens)s,
    cache_hit_input_tokens = %(cache_hit_input_tokens)s,
    cache_miss_input_tokens = %(cache_miss_input_tokens)s,
    cache_write_input_tokens = %(cache_write_input_tokens)s,
    unattributed_input_tokens = %(unattributed_input_tokens)s,
    output_tokens = %(output_tokens)s,
    reasoning_output_tokens = %(reasoning_output_tokens)s,
    total_tokens = %(total_tokens)s,
    usage_available = %(usage_available)s,
    cache_attribution_complete = %(cache_attribution_complete)s,
    completed_at = now(),
    updated_at = now()
WHERE id = %(id)s::uuid
"""


class ModelCallLedger:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @staticmethod
    def _start_parameters(
        call_id: uuid.UUID,
        owner: UsageOwner,
        stage: ModelCallStage,
        model: str,
        provider_attempt: int,
    ) -> dict[str, object]:
        if provider_attempt < 1 or not model or len(model) > 128:
            raise UsagePersistenceError
        return {
            "id": call_id,
            "query_run_id": owner.query_run_id,
            "ingestion_attempt_id": owner.ingestion_attempt_id,
            "trace_id": owner.trace_id,
            "stage": stage.value,
            "model": model,
            "provider_attempt": provider_attempt,
        }

    @staticmethod
    def _finish_parameters(
        call: StartedModelCall,
        outcome: ModelCallOutcome,
        finish_reason: str | None,
        usage: TokenUsage,
    ) -> dict[str, object]:
        safe_finish_reason = (
            finish_reason
            if finish_reason is not None
            and len(finish_reason) <= 80
            and _SAFE_SCALAR.fullmatch(finish_reason)
            else None
        )
        return {
            "id": call.id,
            "outcome": outcome.value,
            "finish_reason": safe_finish_reason,
            "input_tokens": usage.input_tokens,
            "cache_hit_input_tokens": usage.cache_hit_input_tokens,
            "cache_miss_input_tokens": usage.cache_miss_input_tokens,
            "cache_write_input_tokens": usage.cache_write_input_tokens,
            "unattributed_input_tokens": usage.unattributed_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_output_tokens": usage.reasoning_output_tokens,
            "total_tokens": usage.total_tokens,
            "usage_available": usage.usage_available,
            "cache_attribution_complete": usage.cache_attribution_complete,
        }

    def start(
        self, stage: ModelCallStage, model: str, provider_attempt: int
    ) -> StartedModelCall | None:
        owner = _OWNER.get()
        if owner is None:
            return None
        call = StartedModelCall(uuid.uuid4(), owner, stage, model, provider_attempt)
        parameters = self._start_parameters(call.id, owner, stage, model, provider_attempt)
        try:
            with psycopg.connect(self.database_url) as connection:
                inserted = connection.execute(_INSERT_CALL, parameters).fetchone()
                if inserted is None:
                    raise UsagePersistenceError
        except UsagePersistenceError:
            raise
        except Exception as error:
            raise UsagePersistenceError from error
        return call

    async def astart(
        self, stage: ModelCallStage, model: str, provider_attempt: int
    ) -> StartedModelCall | None:
        owner = _OWNER.get()
        if owner is None:
            return None
        call = StartedModelCall(uuid.uuid4(), owner, stage, model, provider_attempt)
        parameters = self._start_parameters(call.id, owner, stage, model, provider_attempt)
        try:
            async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
                cursor = await connection.execute(_INSERT_CALL, parameters)
                inserted = await cursor.fetchone()
                if inserted is None:
                    raise UsagePersistenceError
        except UsagePersistenceError:
            raise
        except Exception as error:
            raise UsagePersistenceError from error
        return call

    def finish(
        self,
        call: StartedModelCall | None,
        outcome: ModelCallOutcome,
        finish_reason: str | None,
        usage: TokenUsage,
    ) -> None:
        if call is None:
            return
        parameters = self._finish_parameters(call, outcome, finish_reason, usage)
        last_error: Exception | None = None
        for _ in range(3):
            try:
                with psycopg.connect(self.database_url) as connection:
                    cursor = connection.execute(_FINALIZE_CALL, parameters)
                    if cursor.rowcount != 1:
                        raise UsagePersistenceError
                return
            except UsagePersistenceError:
                raise
            except (psycopg.OperationalError, psycopg.InterfaceError) as error:
                last_error = error
            except Exception as error:
                raise UsagePersistenceError from error
        raise UsagePersistenceError from last_error

    async def afinish(
        self,
        call: StartedModelCall | None,
        outcome: ModelCallOutcome,
        finish_reason: str | None,
        usage: TokenUsage,
    ) -> None:
        if call is None:
            return
        parameters = self._finish_parameters(call, outcome, finish_reason, usage)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
                    cursor = await connection.execute(_FINALIZE_CALL, parameters)
                    if cursor.rowcount != 1:
                        raise UsagePersistenceError
                return
            except UsagePersistenceError:
                raise
            except (psycopg.OperationalError, psycopg.InterfaceError) as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(0)
            except Exception as error:
                raise UsagePersistenceError from error
        raise UsagePersistenceError from last_error
