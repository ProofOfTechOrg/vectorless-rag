from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import cast

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from vectorless_rag.db import driver_free_url
from vectorless_rag.models import ModelCallOutcome, ModelCallStage
from vectorless_rag.provider import DEEPSEEK_MAX_INPUT_TOKENS, DEEPSEEK_MAX_OUTPUT_TOKENS

_SAFE_SCALAR = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")
_OWNER: ContextVar[UsageOwner | None] = ContextVar("model_call_owner", default=None)
PROVIDER_REQUEST_BYTE_OVERHEAD = 4_096


class UsagePersistenceError(RuntimeError):
    failure_code = "usage_persistence_failed"
    diagnostic = "Model usage accounting could not be persisted."

    def __init__(self) -> None:
        super().__init__(self.diagnostic)


class ExperimentBudgetExceeded(RuntimeError):
    failure_code = "experiment_cost_cap"
    diagnostic = "The frozen experiment cost cap was reached before the next model call."
    retryable = False

    def __init__(self) -> None:
        super().__init__(self.diagnostic)


@dataclass(frozen=True)
class UsageOwner:
    trace_id: str
    query_run_id: uuid.UUID | None = None
    ingestion_attempt_id: uuid.UUID | None = None
    require_cost_reservation: bool = False

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


def provider_input_token_limit(payload: str | bytes) -> int:
    encoded = payload.encode() if isinstance(payload, str) else payload
    return min(
        DEEPSEEK_MAX_INPUT_TOKENS,
        max(1, len(encoded) + PROVIDER_REQUEST_BYTE_OVERHEAD),
    )


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
        ordinary_input_tokens = (
            self.cache_miss_input_tokens
            - self.cache_write_input_tokens
            + self.unattributed_input_tokens
        )
        ordinary_output_tokens = self.output_tokens - self.reasoning_output_tokens
        return {
            "input": ordinary_input_tokens,
            "cache_read_input_tokens": self.cache_hit_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output": ordinary_output_tokens,
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
    logical_call_id: uuid.UUID
    cost_reservation_id: uuid.UUID | None = None


RESERVE_MODEL_CALL_SQL = """
WITH owner_experiment AS (
    SELECT j.experiment_id, 'ingestion' AS owner_kind
    FROM agent.ingestion_attempts a
    JOIN agent.ingestion_jobs j ON j.id = a.job_id
    WHERE a.id = %(ingestion_attempt_id)s::uuid
      AND j.experiment_id IS NOT NULL
    UNION ALL
    SELECT m.experiment_id, 'query' AS owner_kind
    FROM agent.experiment_memberships m
    WHERE m.query_run_id = %(query_run_id)s::uuid
    ORDER BY 1
    LIMIT 1
),
experiment AS (
    SELECT e.*, o.owner_kind
    FROM agent.experiments e
    JOIN owner_experiment o ON o.experiment_id = e.id
    WHERE e.valid
),
inserted AS (
    INSERT INTO agent.model_call_cost_reservations (
        id, experiment_id, logical_call_id, reserved_cost_usd,
        input_token_limit, output_token_limit, attempt_limit, status
    )
    SELECT
        %(reservation_id)s::uuid,
        e.id,
        %(logical_call_id)s::uuid,
        ceil(
            (
                %(input_token_limit)s
                * greatest(
                    (e.price_snapshot->>'input_per_million_usd')::numeric,
                    (e.price_snapshot->>'cache_hit_input_per_million_usd')::numeric
                )
                + %(output_token_limit)s
                * (e.price_snapshot->>'output_per_million_usd')::numeric
            )
            * %(attempt_limit)s
        ) / 1000000,
        %(input_token_limit)s,
        %(output_token_limit)s,
        %(attempt_limit)s,
        'reserved'
    FROM experiment e
    WHERE %(input_token_limit)s > 0
      AND %(output_token_limit)s > 0
      AND %(attempt_limit)s > 0
      AND %(input_token_limit)s <= coalesce(
        (e.price_snapshot->>'max_input_tokens')::numeric,
        %(legacy_max_input_tokens)s
      )
      AND %(output_token_limit)s <= coalesce(
        (e.price_snapshot->>'max_output_tokens')::numeric,
        %(legacy_max_output_tokens)s
      )
      AND %(attempt_limit)s <= coalesce(
        (e.price_snapshot->>'max_attempts')::numeric,
        CASE
            WHEN e.owner_kind = 'query' THEN %(legacy_query_max_attempts)s
        END
      )
      AND NOT EXISTS (
        SELECT 1
        FROM agent.model_call_cost_reservations r
        WHERE r.experiment_id = e.id
          AND r.logical_call_id = %(logical_call_id)s::uuid
    )
    RETURNING id, input_token_limit, output_token_limit, attempt_limit
),
resolved AS (
    SELECT id, input_token_limit, output_token_limit, attempt_limit FROM inserted
    UNION ALL
    SELECT r.id, r.input_token_limit, r.output_token_limit, r.attempt_limit
    FROM agent.model_call_cost_reservations r
    JOIN experiment e ON e.id = r.experiment_id
    WHERE r.logical_call_id = %(logical_call_id)s::uuid
)
SELECT id, input_token_limit, output_token_limit, attempt_limit FROM resolved
UNION ALL
SELECT NULL::uuid, NULL::bigint, NULL::integer, NULL::integer
FROM owner_experiment
WHERE NOT EXISTS (SELECT 1 FROM resolved)
LIMIT 1
"""

_INSERT_CALL = """
INSERT INTO agent.model_calls (
    id, query_run_id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
    logical_call_id, operation, retry_delay_ms, prompt_sha256, prompt_bytes,
    release, configuration_hash, experiment_id, cost_reservation_id,
    outcome, usage_available, cache_attribution_complete
)
SELECT
    %(id)s::uuid, %(query_run_id)s::uuid, %(ingestion_attempt_id)s::uuid, %(trace_id)s,
    %(stage)s::agent.model_call_stage, %(model)s, %(provider_attempt)s,
    %(logical_call_id)s::uuid, %(operation)s, %(retry_delay_ms)s,
    %(prompt_sha256)s, %(prompt_bytes)s, %(release)s, %(configuration_hash)s,
    (
      SELECT experiment_id
      FROM agent.model_call_cost_reservations
      WHERE id = %(cost_reservation_id)s::uuid
    ),
    %(cost_reservation_id)s::uuid,
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
    http_status = %(http_status)s,
    failure_code = %(failure_code)s,
    retryable = %(retryable)s,
    latency_ms = %(latency_ms)s,
    output_sha256 = %(output_sha256)s,
    output_bytes = %(output_bytes)s,
    cancelled = %(cancelled)s,
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

RECONCILE_MODEL_CALL_RESERVATION_SQL = """
WITH measured AS (
    SELECT
        r.id,
        (
            r.input_token_limit
            * greatest(
                (e.price_snapshot->>'input_per_million_usd')::numeric,
                (e.price_snapshot->>'cache_hit_input_per_million_usd')::numeric
            )
            + r.output_token_limit
            * (e.price_snapshot->>'output_per_million_usd')::numeric
        ) / 1000000 AS per_attempt_cost_limit,
        sum(
            (
                coalesce(c.cache_hit_input_tokens, 0)
                * (e.price_snapshot->>'cache_hit_input_per_million_usd')::numeric
                + (
                    coalesce(c.cache_miss_input_tokens, 0)
                    + coalesce(c.unattributed_input_tokens, 0)
                )
                * (e.price_snapshot->>'input_per_million_usd')::numeric
                + coalesce(c.output_tokens, 0)
                * (e.price_snapshot->>'output_per_million_usd')::numeric
            ) / 1000000
        ) FILTER (WHERE c.usage_available) AS measured_cost,
        bool_and(c.usage_available) FILTER (WHERE c.outcome <> 'started') AS usage_complete,
        count(*) FILTER (
            WHERE c.outcome <> 'started' AND NOT c.usage_available
        ) AS unknown_attempts
    FROM agent.model_call_cost_reservations r
    JOIN agent.experiments e ON e.id = r.experiment_id
    LEFT JOIN agent.model_calls c ON c.cost_reservation_id = r.id
    WHERE r.id = %(cost_reservation_id)s::uuid
    GROUP BY r.id, e.price_snapshot
    HAVING coalesce(
        bool_and(
            c.id IS NULL
            OR (
                c.logical_call_id = r.logical_call_id
                AND c.provider_attempt BETWEEN 1 AND r.attempt_limit
            )
        ),
        true
    )
    AND count(c.id) = count(DISTINCT c.provider_attempt)
)
UPDATE agent.model_call_cost_reservations r
SET measured_cost_usd = CASE
        WHEN measured.measured_cost IS NULL THEN NULL
        ELSE ceil(measured.measured_cost * 1000000) / 1000000
    END,
    reserved_cost_usd = CASE
        WHEN %(terminal)s AND NOT coalesce(measured.usage_complete, false)
        THEN ceil(
            (
                coalesce(measured.measured_cost, 0)
                + measured.unknown_attempts * measured.per_attempt_cost_limit
            )
            * 1000000
        ) / 1000000
        ELSE r.reserved_cost_usd
    END,
    status = CASE
        WHEN NOT %(terminal)s THEN 'reserved'
        WHEN coalesce(measured.usage_complete, false) THEN 'finalized'
        ELSE 'review_required'
    END,
    updated_at = now()
FROM measured
WHERE r.id = measured.id
RETURNING r.id
"""

_SYNC_POOLS: dict[str, ConnectionPool[psycopg.Connection[tuple[object, ...]]]] = {}
_SYNC_POOLS_LOCK = threading.Lock()


def _sync_pool(
    database_url: str,
) -> ConnectionPool[psycopg.Connection[tuple[object, ...]]]:
    with _SYNC_POOLS_LOCK:
        pool = _SYNC_POOLS.get(database_url)
        if pool is None:
            pool = cast(
                ConnectionPool[psycopg.Connection[tuple[object, ...]]],
                ConnectionPool(
                    conninfo=database_url,
                    min_size=0,
                    max_size=5,
                    open=True,
                    name="model-call-ledger",
                ),
            )
            _SYNC_POOLS[database_url] = pool
        return pool


class ModelCallLedger:
    def __init__(self, database_url: str, *, maximum_provider_attempts: int = 3) -> None:
        self.database_url = driver_free_url(database_url)
        self.maximum_provider_attempts = maximum_provider_attempts
        self._pool = _sync_pool(self.database_url)
        self._async_pool: AsyncConnectionPool[psycopg.AsyncConnection[tuple[object, ...]]] = (
            AsyncConnectionPool(
                conninfo=self.database_url,
                min_size=0,
                max_size=5,
                open=False,
                name="async-model-call-ledger",
            )
        )
        self._async_open_lock = asyncio.Lock()

    @staticmethod
    def _start_parameters(
        call_id: uuid.UUID,
        owner: UsageOwner,
        stage: ModelCallStage,
        model: str,
        provider_attempt: int,
        *,
        logical_call_id: uuid.UUID,
        operation: str,
        prompt: str | bytes | None,
        release: str | None,
        configuration_hash: str | None,
        retry_delay_ms: int | None,
    ) -> dict[str, object]:
        if (
            provider_attempt < 1
            or not model
            or len(model) > 128
            or not _SAFE_SCALAR.fullmatch(operation)
            or (retry_delay_ms is not None and retry_delay_ms < 0)
        ):
            raise UsagePersistenceError
        prompt_bytes = prompt.encode() if isinstance(prompt, str) else prompt
        return {
            "id": call_id,
            "query_run_id": owner.query_run_id,
            "ingestion_attempt_id": owner.ingestion_attempt_id,
            "trace_id": owner.trace_id,
            "stage": stage.value,
            "model": model,
            "provider_attempt": provider_attempt,
            "logical_call_id": logical_call_id,
            "operation": operation,
            "retry_delay_ms": retry_delay_ms,
            "prompt_sha256": (
                hashlib.sha256(prompt_bytes).hexdigest() if prompt_bytes is not None else None
            ),
            "prompt_bytes": len(prompt_bytes) if prompt_bytes is not None else None,
            "release": (
                release
                if release is not None and len(release) <= 128 and _SAFE_SCALAR.fullmatch(release)
                else None
            ),
            "configuration_hash": (
                configuration_hash
                if configuration_hash is not None
                and re.fullmatch(r"[0-9a-f]{64}", configuration_hash)
                else None
            ),
        }

    @staticmethod
    def _finish_parameters(
        call: StartedModelCall,
        outcome: ModelCallOutcome,
        finish_reason: str | None,
        usage: TokenUsage,
        *,
        failure_code: str | None,
        retryable: bool | None,
        http_status: int | None,
        latency_ms: int | None,
        output: str | bytes | None,
        cancelled: bool,
    ) -> dict[str, object]:
        safe_finish_reason = (
            finish_reason
            if finish_reason is not None
            and len(finish_reason) <= 80
            and _SAFE_SCALAR.fullmatch(finish_reason)
            else None
        )
        safe_failure = (
            failure_code
            if failure_code is not None
            and len(failure_code) <= 80
            and _SAFE_SCALAR.fullmatch(failure_code)
            else None
        )
        output_bytes = output.encode() if isinstance(output, str) else output
        return {
            "id": call.id,
            "outcome": outcome.value,
            "finish_reason": safe_finish_reason,
            "failure_code": safe_failure,
            "retryable": retryable,
            "http_status": (
                http_status if http_status is None or 100 <= http_status <= 599 else None
            ),
            "latency_ms": latency_ms if latency_ms is None or latency_ms >= 0 else None,
            "output_sha256": (
                hashlib.sha256(output_bytes).hexdigest() if output_bytes is not None else None
            ),
            "output_bytes": len(output_bytes) if output_bytes is not None else None,
            "cancelled": cancelled,
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

    def _reservation_parameters(
        self,
        owner: UsageOwner,
        reservation_id: uuid.UUID,
        logical_call_id: uuid.UUID,
        input_token_limit: int,
        output_token_limit: int,
    ) -> dict[str, object]:
        if (
            input_token_limit < 1
            or input_token_limit > DEEPSEEK_MAX_INPUT_TOKENS
            or output_token_limit < 1
            or output_token_limit > DEEPSEEK_MAX_OUTPUT_TOKENS
        ):
            raise UsagePersistenceError
        return {
            "query_run_id": owner.query_run_id,
            "ingestion_attempt_id": owner.ingestion_attempt_id,
            "reservation_id": reservation_id,
            "logical_call_id": logical_call_id,
            "input_token_limit": input_token_limit,
            "output_token_limit": output_token_limit,
            "attempt_limit": self.maximum_provider_attempts,
            "legacy_max_input_tokens": DEEPSEEK_MAX_INPUT_TOKENS,
            "legacy_max_output_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "legacy_query_max_attempts": self.maximum_provider_attempts,
        }

    def start(
        self,
        stage: ModelCallStage,
        model: str,
        provider_attempt: int,
        *,
        logical_call_id: uuid.UUID | None = None,
        operation: str | None = None,
        prompt: str | bytes | None = None,
        release: str | None = None,
        configuration_hash: str | None = None,
        retry_delay_ms: int | None = None,
        input_token_limit: int,
        output_token_limit: int,
    ) -> StartedModelCall | None:
        if provider_attempt > self.maximum_provider_attempts:
            raise UsagePersistenceError
        owner = _OWNER.get()
        if owner is None:
            return None
        logical_call_id = logical_call_id or uuid.uuid4()
        call_id = uuid.uuid4()
        reservation_id = uuid.uuid4()
        parameters = self._start_parameters(
            call_id,
            owner,
            stage,
            model,
            provider_attempt,
            logical_call_id=logical_call_id,
            operation=operation or stage.value,
            prompt=prompt,
            release=release,
            configuration_hash=configuration_hash,
            retry_delay_ms=retry_delay_ms,
        )
        try:
            with self._pool.connection() as connection:
                reservation = connection.execute(
                    RESERVE_MODEL_CALL_SQL,
                    self._reservation_parameters(
                        owner,
                        reservation_id,
                        logical_call_id,
                        input_token_limit,
                        output_token_limit,
                    ),
                ).fetchone()
                if owner.require_cost_reservation and reservation is None:
                    raise ExperimentBudgetExceeded
                if reservation is not None and reservation[0] is None:
                    raise ExperimentBudgetExceeded
                if reservation is not None and tuple(reservation[1:]) != (
                    input_token_limit,
                    output_token_limit,
                    self.maximum_provider_attempts,
                ):
                    raise UsagePersistenceError
                cost_reservation_id = (
                    uuid.UUID(str(reservation[0])) if reservation is not None else None
                )
                parameters["cost_reservation_id"] = cost_reservation_id
                inserted = connection.execute(_INSERT_CALL, parameters).fetchone()
                if inserted is None:
                    raise UsagePersistenceError
                call = StartedModelCall(
                    call_id,
                    owner,
                    stage,
                    model,
                    provider_attempt,
                    logical_call_id,
                    cost_reservation_id,
                )
        except Exception as error:
            if "experiment cost cap exceeded" in str(error):
                raise ExperimentBudgetExceeded from error
            if isinstance(error, ExperimentBudgetExceeded):
                raise
            if isinstance(error, UsagePersistenceError):
                raise
            raise UsagePersistenceError from error
        return call

    async def astart(
        self,
        stage: ModelCallStage,
        model: str,
        provider_attempt: int,
        *,
        logical_call_id: uuid.UUID | None = None,
        operation: str | None = None,
        prompt: str | bytes | None = None,
        release: str | None = None,
        configuration_hash: str | None = None,
        retry_delay_ms: int | None = None,
        input_token_limit: int,
        output_token_limit: int,
    ) -> StartedModelCall | None:
        if provider_attempt > self.maximum_provider_attempts:
            raise UsagePersistenceError
        owner = _OWNER.get()
        if owner is None:
            return None
        logical_call_id = logical_call_id or uuid.uuid4()
        call_id = uuid.uuid4()
        reservation_id = uuid.uuid4()
        parameters = self._start_parameters(
            call_id,
            owner,
            stage,
            model,
            provider_attempt,
            logical_call_id=logical_call_id,
            operation=operation or stage.value,
            prompt=prompt,
            release=release,
            configuration_hash=configuration_hash,
            retry_delay_ms=retry_delay_ms,
        )
        try:
            async with self._async_open_lock:
                if self._async_pool.closed:
                    await self._async_pool.open()
            async with self._async_pool.connection() as connection:
                reservation_cursor = await connection.execute(
                    RESERVE_MODEL_CALL_SQL,
                    self._reservation_parameters(
                        owner,
                        reservation_id,
                        logical_call_id,
                        input_token_limit,
                        output_token_limit,
                    ),
                )
                reservation = await reservation_cursor.fetchone()
                if owner.require_cost_reservation and reservation is None:
                    raise ExperimentBudgetExceeded
                if reservation is not None and reservation[0] is None:
                    raise ExperimentBudgetExceeded
                if reservation is not None and tuple(reservation[1:]) != (
                    input_token_limit,
                    output_token_limit,
                    self.maximum_provider_attempts,
                ):
                    raise UsagePersistenceError
                cost_reservation_id = (
                    uuid.UUID(str(reservation[0])) if reservation is not None else None
                )
                parameters["cost_reservation_id"] = cost_reservation_id
                cursor = await connection.execute(_INSERT_CALL, parameters)
                inserted = await cursor.fetchone()
                if inserted is None:
                    raise UsagePersistenceError
                call = StartedModelCall(
                    call_id,
                    owner,
                    stage,
                    model,
                    provider_attempt,
                    logical_call_id,
                    cost_reservation_id,
                )
        except Exception as error:
            if "experiment cost cap exceeded" in str(error):
                raise ExperimentBudgetExceeded from error
            if isinstance(error, ExperimentBudgetExceeded):
                raise
            if isinstance(error, UsagePersistenceError):
                raise
            raise UsagePersistenceError from error
        return call

    def finish(
        self,
        call: StartedModelCall | None,
        outcome: ModelCallOutcome,
        finish_reason: str | None,
        usage: TokenUsage,
        *,
        failure_code: str | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        latency_ms: int | None = None,
        output: str | bytes | None = None,
        cancelled: bool = False,
    ) -> None:
        if call is None:
            return
        parameters = self._finish_parameters(
            call,
            outcome,
            finish_reason,
            usage,
            failure_code=failure_code,
            retryable=retryable,
            http_status=http_status,
            latency_ms=latency_ms,
            output=output,
            cancelled=cancelled,
        )
        last_error: Exception | None = None
        for _ in range(3):
            try:
                with self._pool.connection() as connection:
                    cursor = connection.execute(_FINALIZE_CALL, parameters)
                    if cursor.rowcount != 1:
                        raise UsagePersistenceError
                    if call.cost_reservation_id is not None:
                        reconciled = connection.execute(
                            RECONCILE_MODEL_CALL_RESERVATION_SQL,
                            {
                                "cost_reservation_id": call.cost_reservation_id,
                                "terminal": (
                                    outcome
                                    in {
                                        ModelCallOutcome.completed,
                                        ModelCallOutcome.cancelled,
                                        ModelCallOutcome.abandoned,
                                    }
                                    or retryable is False
                                    or call.provider_attempt >= self.maximum_provider_attempts
                                ),
                            },
                        ).fetchone()
                        if reconciled is None:
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
        *,
        failure_code: str | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        latency_ms: int | None = None,
        output: str | bytes | None = None,
        cancelled: bool = False,
    ) -> None:
        if call is None:
            return
        parameters = self._finish_parameters(
            call,
            outcome,
            finish_reason,
            usage,
            failure_code=failure_code,
            retryable=retryable,
            http_status=http_status,
            latency_ms=latency_ms,
            output=output,
            cancelled=cancelled,
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self._async_open_lock:
                    if self._async_pool.closed:
                        await self._async_pool.open()
                async with self._async_pool.connection() as connection:
                    cursor = await connection.execute(_FINALIZE_CALL, parameters)
                    if cursor.rowcount != 1:
                        raise UsagePersistenceError
                    if call.cost_reservation_id is not None:
                        reconcile_cursor = await connection.execute(
                            RECONCILE_MODEL_CALL_RESERVATION_SQL,
                            {
                                "cost_reservation_id": call.cost_reservation_id,
                                "terminal": (
                                    outcome
                                    in {
                                        ModelCallOutcome.completed,
                                        ModelCallOutcome.cancelled,
                                        ModelCallOutcome.abandoned,
                                    }
                                    or retryable is False
                                    or call.provider_attempt >= self.maximum_provider_attempts
                                ),
                            },
                        )
                        if await reconcile_cursor.fetchone() is None:
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
