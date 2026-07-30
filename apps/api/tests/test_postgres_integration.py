from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar, cast

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors
from pydantic import BaseModel
from sqlalchemy import Numeric, delete, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from vectorless_rag.config import Settings
from vectorless_rag.db import (
    claim_ingestion_job,
    make_engine,
    set_ingestion_provider_admission,
)
from vectorless_rag.graph import (
    _normalize_short_regex_terms,  # pyright: ignore[reportPrivateUsage]
)
from vectorless_rag.models import (
    Document,
    DocumentStatus,
    Experiment,
    IndexLifecycleStatus,
    IndexVersion,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    JobStatus,
    ModelCallCostReservation,
    ModelCallOutcome,
    ModelCallStage,
)
from vectorless_rag.provider import DEEPSEEK_MAX_INPUT_TOKENS, DEEPSEEK_MAX_OUTPUT_TOKENS
from vectorless_rag.retrieval import CorpusRouter
from vectorless_rag.schemas import MetadataConstraints
from vectorless_rag.sql_guard import PostgreSQLAdapter, SQLRejected, validate_sql
from vectorless_rag.telemetry_classification import classification_report
from vectorless_rag.usage import (
    RECONCILE_MODEL_CALL_RESERVATION_SQL,
    ExperimentBudgetExceeded,
    ModelCallLedger,
    StartedModelCall,
    TokenUsage,
    UsageOwner,
    UsagePersistenceError,
    usage_context,
)

OWNER_URL = os.getenv("TEST_OWNER_DATABASE_URL")
APP_URL = os.getenv("TEST_APP_DATABASE_URL")
READER_URL = os.getenv("TEST_DATABASE_URL")
DISPOSABLE_DATABASE = os.getenv("TEST_DATABASE_DISPOSABLE") == "1"
StructuredT = TypeVar("StructuredT", bound=BaseModel)
API_ROOT = Path(__file__).resolve().parents[1]
_RESERVE_MODEL_CALL_SQL = """
SELECT *
FROM agent.reserve_model_call(
    %(query_run_id)s::uuid,
    %(ingestion_attempt_id)s::uuid,
    %(reservation_id)s::uuid,
    %(logical_call_id)s::uuid,
    %(input_token_limit)s::bigint,
    %(output_token_limit)s::integer,
    %(attempt_limit)s::integer,
    %(legacy_max_input_tokens)s::bigint,
    %(legacy_max_output_tokens)s::integer,
    %(legacy_query_max_attempts)s::integer
)
"""


def _starts_model_call(query: object) -> bool:
    return "agent.start_model_call" in str(query)


class _PauseAfterCommitContext:
    def __init__(
        self,
        context: Any,
        committed: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.context = context
        self.committed = committed
        self.release = release

    async def __aenter__(self) -> Any:
        return await self.context.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Any:
        result = await self.context.__aexit__(exc_type, exc, traceback)
        self.committed.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.release.set()
            raise
        return result


class _SignalExecuteConnection:
    def __init__(self, connection: Any, executing: asyncio.Event) -> None:
        self.connection = connection
        self.executing = executing

    async def execute(self, query: object, parameters: object) -> Any:
        self.executing.set()
        return await self.connection.execute(query, parameters)


class _SignalExecuteContext:
    def __init__(self, context: Any, executing: asyncio.Event) -> None:
        self.context = context
        self.executing = executing

    async def __aenter__(self) -> _SignalExecuteConnection:
        return _SignalExecuteConnection(await self.context.__aenter__(), self.executing)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Any:
        return await self.context.__aexit__(exc_type, exc, traceback)


class _PauseAfterCommitPool:
    def __init__(
        self,
        pool: Any,
        committed: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.pool = pool
        self.committed = committed
        self.release = release

    @property
    def closed(self) -> bool:
        return bool(self.pool.closed)

    async def open(self) -> None:
        await self.pool.open()

    def connection(self) -> Any:
        return _PauseAfterCommitContext(
            self.pool.connection(),
            self.committed,
            self.release,
        )


class _SignalExecutePool:
    def __init__(self, pool: Any, executing: asyncio.Event) -> None:
        self.pool = pool
        self.executing = executing

    @property
    def closed(self) -> bool:
        return bool(self.pool.closed)

    async def open(self) -> None:
        await self.pool.open()

    def connection(self) -> Any:
        return _SignalExecuteContext(self.pool.connection(), self.executing)


class _PauseBeforeCallInsertConnection:
    def __init__(
        self,
        connection: Any,
        blocked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.connection = connection
        self.blocked = blocked
        self.release = release

    async def execute(self, query: object, parameters: object) -> Any:
        if _starts_model_call(query) and not self.release.is_set():
            self.blocked.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.release.set()
                raise
        return await self.connection.execute(query, parameters)


class _PauseBeforeCallInsertContext:
    def __init__(
        self,
        context: Any,
        blocked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.context = context
        self.blocked = blocked
        self.release = release

    async def __aenter__(self) -> _PauseBeforeCallInsertConnection:
        return _PauseBeforeCallInsertConnection(
            await self.context.__aenter__(),
            self.blocked,
            self.release,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Any:
        return await self.context.__aexit__(exc_type, exc, traceback)


class _PauseBeforeCallInsertPool:
    def __init__(
        self,
        pool: Any,
        blocked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.pool = pool
        self.blocked = blocked
        self.release = release

    @property
    def closed(self) -> bool:
        return bool(self.pool.closed)

    async def open(self) -> None:
        await self.pool.open()

    def connection(self) -> Any:
        return _PauseBeforeCallInsertContext(
            self.pool.connection(),
            self.blocked,
            self.release,
        )


class _PauseAfterCallInsertConnection:
    def __init__(
        self,
        connection: Any,
        inserted: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.connection = connection
        self.inserted = inserted
        self.release = release

    async def execute(self, query: object, parameters: object) -> Any:
        result = await self.connection.execute(query, parameters)
        if _starts_model_call(query):
            self.inserted.set()
            await self.release.wait()
        return result


class _PauseAfterCallInsertContext:
    def __init__(
        self,
        context: Any,
        inserted: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.context = context
        self.inserted = inserted
        self.release = release

    async def __aenter__(self) -> _PauseAfterCallInsertConnection:
        return _PauseAfterCallInsertConnection(
            await self.context.__aenter__(),
            self.inserted,
            self.release,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Any:
        return await self.context.__aexit__(exc_type, exc, traceback)


class _PauseAfterCallInsertPool:
    def __init__(
        self,
        pool: Any,
        inserted: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.pool = pool
        self.inserted = inserted
        self.release = release

    @property
    def closed(self) -> bool:
        return bool(self.pool.closed)

    async def open(self) -> None:
        await self.pool.open()

    def connection(self) -> Any:
        return _PauseAfterCallInsertContext(
            self.pool.connection(),
            self.inserted,
            self.release,
        )


class _PauseCleanupAfterReservationConnection:
    def __init__(
        self,
        connection: Any,
        insert_blocked: asyncio.Event,
        insert_release: asyncio.Event,
        cleanup_locked: asyncio.Event,
        cleanup_release: asyncio.Event,
    ) -> None:
        self.connection = connection
        self.insert_blocked = insert_blocked
        self.insert_release = insert_release
        self.cleanup_locked = cleanup_locked
        self.cleanup_release = cleanup_release

    async def execute(self, query: object, parameters: object) -> Any:
        statement = str(query)
        if _starts_model_call(query) and not self.insert_release.is_set():
            self.insert_blocked.set()
            try:
                await self.insert_release.wait()
            except asyncio.CancelledError:
                self.insert_release.set()
                raise
        result = await self.connection.execute(query, parameters)
        if statement.lstrip().startswith("WITH cancelled AS"):
            self.cleanup_locked.set()
            await self.cleanup_release.wait()
        return result


class _PauseCleanupAfterReservationContext:
    def __init__(
        self,
        context: Any,
        insert_blocked: asyncio.Event,
        insert_release: asyncio.Event,
        cleanup_locked: asyncio.Event,
        cleanup_release: asyncio.Event,
    ) -> None:
        self.context = context
        self.insert_blocked = insert_blocked
        self.insert_release = insert_release
        self.cleanup_locked = cleanup_locked
        self.cleanup_release = cleanup_release

    async def __aenter__(self) -> _PauseCleanupAfterReservationConnection:
        return _PauseCleanupAfterReservationConnection(
            await self.context.__aenter__(),
            self.insert_blocked,
            self.insert_release,
            self.cleanup_locked,
            self.cleanup_release,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Any:
        return await self.context.__aexit__(exc_type, exc, traceback)


class _PauseCleanupAfterReservationPool:
    def __init__(
        self,
        pool: Any,
        insert_blocked: asyncio.Event,
        insert_release: asyncio.Event,
        cleanup_locked: asyncio.Event,
        cleanup_release: asyncio.Event,
    ) -> None:
        self.pool = pool
        self.insert_blocked = insert_blocked
        self.insert_release = insert_release
        self.cleanup_locked = cleanup_locked
        self.cleanup_release = cleanup_release

    @property
    def closed(self) -> bool:
        return bool(self.pool.closed)

    async def open(self) -> None:
        await self.pool.open()

    def connection(self) -> Any:
        return _PauseCleanupAfterReservationContext(
            self.pool.connection(),
            self.insert_blocked,
            self.insert_release,
            self.cleanup_locked,
            self.cleanup_release,
        )


@dataclass(frozen=True)
class _ReservationTestState:
    owner_sync: str
    experiment_id: uuid.UUID
    attempt_id: uuid.UUID
    query_run_id: uuid.UUID


def _insert_reservation_test_experiment(
    connection: psycopg.Connection[tuple[Any, ...]],
    experiment_id: uuid.UUID,
    name: str,
) -> None:
    connection.execute(
        """INSERT INTO agent.experiments
           (id, name, release_sha, configuration_hash, corpus_hash, index_hash,
            dataset_hash, price_snapshot, repetitions, evidence_token_budget,
            seed, maximum_cost_usd, frozen_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 1, 6400, 1, 10, now())""",
        (
            experiment_id,
            name,
            "a" * 40,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            json.dumps(
                {
                    "input_per_million_usd": 1,
                    "cache_hit_input_per_million_usd": 0.1,
                    "output_per_million_usd": 2,
                    "max_input_tokens": 100,
                    "max_output_tokens": 100,
                    "max_attempts": 3,
                }
            ),
        ),
    )


@pytest.fixture
def reservation_test_state() -> Iterator[_ReservationTestState]:
    assert OWNER_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    experiment_id = uuid.uuid4()
    api_key_id = uuid.uuid4()
    query_run_id = uuid.uuid4()
    with psycopg.connect(owner_sync) as connection:
        connection.execute(
            """INSERT INTO agent.documents
               (id, sha256, original_filename, pdf_object_key, status)
               VALUES (%s, %s, 'reservation.pdf', 'pdf/reservation', 'indexing')""",
            (document_id, uuid.uuid4().hex + uuid.uuid4().hex),
        )
        _insert_reservation_test_experiment(
            connection,
            experiment_id,
            "ingestion-reservation-test",
        )
        connection.execute(
            """INSERT INTO agent.ingestion_jobs
               (id, document_id, status, force, mode, attempts, retry_budget_consumed,
                is_active, metrics, experiment_id)
               VALUES (%s, %s, 'running', true, 'reextract_all', 1, 0, true,
                       '{}'::jsonb, %s)""",
            (job_id, document_id, experiment_id),
        )
        connection.execute(
            """INSERT INTO agent.ingestion_attempts
               (id, job_id, attempt_number, trace_id, worker_id, status, artifact_action,
                telemetry_complete, retry_budget_consumed)
               VALUES (%s, %s, 1, %s, 'reservation-worker', 'running', 'none',
                       false, false)""",
            (attempt_id, job_id, uuid.uuid4().hex),
        )
        connection.execute(
            """INSERT INTO agent.api_keys
               (id, name, prefix, key_hash, scopes, disabled)
               VALUES (%s, 'unreserved-call-owner', %s, %s, '{}'::text[], true)""",
            (api_key_id, f"t{api_key_id.hex[:8]}", api_key_id.hex + api_key_id.hex),
        )
        connection.execute(
            """INSERT INTO agent.query_runs
               (id, trace_id, thread_id, api_key_id, question)
               VALUES (%s, %s, 'unreserved-call-owner', %s, 'question')""",
            (query_run_id, uuid.uuid4().hex, api_key_id),
        )
        connection.commit()

    try:
        yield _ReservationTestState(
            owner_sync=owner_sync,
            experiment_id=experiment_id,
            attempt_id=attempt_id,
            query_run_id=query_run_id,
        )
    finally:
        with psycopg.connect(owner_sync) as connection:
            memberships = connection.execute(
                """SELECT count(*) FROM agent.experiment_memberships
                   WHERE query_run_id = %s""",
                (query_run_id,),
            ).fetchone()
            if memberships == (0,):
                connection.execute(
                    """DELETE FROM agent.model_calls
                       WHERE ingestion_attempt_id = %s OR query_run_id = %s""",
                    (attempt_id, query_run_id),
                )
                connection.execute(
                    """DELETE FROM agent.model_call_cost_reservations
                       WHERE experiment_id = %s""",
                    (experiment_id,),
                )
                connection.execute(
                    "DELETE FROM agent.query_runs WHERE id = %s",
                    (query_run_id,),
                )
                connection.execute("DELETE FROM agent.api_keys WHERE id = %s", (api_key_id,))
                connection.execute(
                    "DELETE FROM agent.ingestion_attempts WHERE id = %s",
                    (attempt_id,),
                )
                connection.execute(
                    "DELETE FROM agent.ingestion_jobs WHERE id = %s",
                    (job_id,),
                )
                connection.execute(
                    "DELETE FROM agent.experiments WHERE id = %s",
                    (experiment_id,),
                )
                connection.execute(
                    "DELETE FROM agent.documents WHERE id = %s",
                    (document_id,),
                )
                connection.commit()
            else:
                assert DISPOSABLE_DATABASE


async def _create_retryable_reserved_call(
    ledger: ModelCallLedger,
    owner: UsageOwner,
    logical_call_id: uuid.UUID,
    *,
    stage: ModelCallStage = ModelCallStage.pageindex_generation,
) -> StartedModelCall:
    with usage_context(owner):
        call = await ledger.astart(
            stage,
            "provider/model",
            1,
            logical_call_id=logical_call_id,
            input_token_limit=100,
            output_token_limit=1,
        )
    assert call is not None
    await ledger.afinish(
        call,
        ModelCallOutcome.failed,
        None,
        TokenUsage.unavailable(),
        failure_code="provider_rejected",
        retryable=True,
    )
    return call


def _alembic_config() -> Config:
    assert OWNER_URL is not None
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", OWNER_URL)
    return config


@pytest.fixture
def downgraded_measurement_schema() -> Iterator[Config]:
    config = _alembic_config()
    command.downgrade(config, "0005_measurement_experiments")
    try:
        yield config
    finally:
        command.upgrade(config, "head")


def test_cost_orm_columns_match_the_numeric_database_contract() -> None:
    for column in (
        Experiment.__table__.c.maximum_cost_usd,
        ModelCallCostReservation.__table__.c.reserved_cost_usd,
        ModelCallCostReservation.__table__.c.measured_cost_usd,
    ):
        assert isinstance(column.type, Numeric)
        assert column.type.precision == 14
        assert column.type.scale == 6
        assert column.type.asdecimal is True


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not DISPOSABLE_DATABASE,
    reason="disposable PostgreSQL owner URL not set",
)
def test_cost_migrations_backfill_reservation_bounds_and_call_ownership(
    downgraded_measurement_schema: Config,
) -> None:
    assert OWNER_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")

    explicit_experiment = uuid.uuid4()
    legacy_experiment = uuid.uuid4()
    explicit_reservation = uuid.uuid4()
    legacy_reservation = uuid.uuid4()
    explicit_logical_call = uuid.uuid4()
    api_key_id = uuid.uuid4()
    query_run_id = uuid.uuid4()
    model_call_id = uuid.uuid4()
    with psycopg.connect(owner_sync) as connection:
        assert connection.execute(
            "SELECT to_regprocedure('agent.ingestion_attempt_job_immutable()')"
        ).fetchone() == (None,)
        for experiment_id, snapshot in (
            (
                explicit_experiment,
                {
                    "input_per_million_usd": 1,
                    "cache_hit_input_per_million_usd": 0.1,
                    "output_per_million_usd": 2,
                    "max_input_tokens": 1_000_000.0,
                    "max_output_tokens": 8_192.0,
                    "max_attempts": 3.0,
                },
            ),
            (
                legacy_experiment,
                {
                    "input_per_million_usd": 1,
                    "cache_hit_input_per_million_usd": 0.1,
                    "output_per_million_usd": 2,
                },
            ),
        ):
            connection.execute(
                """INSERT INTO agent.experiments
                   (id, name, release_sha, configuration_hash, corpus_hash, index_hash,
                    dataset_hash, price_snapshot, repetitions, evidence_token_budget,
                    seed, maximum_cost_usd, frozen_at)
                   VALUES (%s, 'migration-backfill', %s, %s, %s, %s, %s, %s::jsonb,
                           1, 6400, 1, 10, now())""",
                (
                    experiment_id,
                    "a" * 40,
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    json.dumps(snapshot),
                ),
            )
        connection.execute(
            """INSERT INTO agent.model_call_cost_reservations
               (id, experiment_id, logical_call_id, reserved_cost_usd,
                measured_cost_usd, status)
               VALUES (%s, %s, %s, 1.250000, 0.500000, 'finalized'),
                      (%s, %s, %s, 2.500000, 0.750000, 'review_required')""",
            (
                explicit_reservation,
                explicit_experiment,
                explicit_logical_call,
                legacy_reservation,
                legacy_experiment,
                uuid.uuid4(),
            ),
        )
        connection.execute(
            """INSERT INTO agent.api_keys
               (id, name, prefix, key_hash, scopes, disabled)
               VALUES (%s, 'migration-call-owner', %s, %s, '{}'::text[], true)""",
            (api_key_id, f"t{api_key_id.hex[:8]}", api_key_id.hex + api_key_id.hex),
        )
        connection.execute(
            """INSERT INTO agent.query_runs
               (id, trace_id, thread_id, api_key_id, question)
               VALUES (%s, %s, 'migration-call-owner', %s, 'question')""",
            (query_run_id, uuid.uuid4().hex, api_key_id),
        )
        connection.execute(
            """INSERT INTO agent.experiment_memberships
               (id, experiment_id, query_run_id, trial_key, repetition,
                sequence_number, included)
               VALUES (%s, %s, %s, %s, 1, 0, true)""",
            (uuid.uuid4(), explicit_experiment, query_run_id, str(uuid.uuid4())),
        )
        connection.execute(
            """INSERT INTO agent.model_calls
               (id, query_run_id, trace_id, stage, model, provider_attempt,
                logical_call_id, operation, cost_reservation_id, outcome,
                usage_available, cache_attribution_complete)
               VALUES (%s, %s, %s, 'query_route', 'test', 1, %s,
                       'query_route', %s, 'failed', false, false)""",
            (
                model_call_id,
                query_run_id,
                uuid.uuid4().hex,
                explicit_logical_call,
                explicit_reservation,
            ),
        )
        assert connection.execute(
            "SELECT experiment_id FROM agent.model_calls WHERE id = %s",
            (model_call_id,),
        ).fetchone() == (None,)
        assert connection.execute(
            """SELECT price_snapshot->>'max_input_tokens',
                      price_snapshot->>'max_output_tokens',
                      price_snapshot->>'max_attempts'
               FROM agent.experiments WHERE id = %s""",
            (explicit_experiment,),
        ).fetchone() == ("1000000.0", "8192.0", "3.0")
        connection.commit()

    command.upgrade(downgraded_measurement_schema, "head")

    with psycopg.connect(owner_sync) as connection:
        assert connection.execute(
            """SELECT input_token_limit, output_token_limit, attempt_limit,
                      reserved_cost_usd, measured_cost_usd, status
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (explicit_reservation,),
        ).fetchone() == (
            1_000_000,
            8_192,
            3,
            Decimal("1.250000"),
            Decimal("0.500000"),
            "finalized",
        )
        assert connection.execute(
            """SELECT input_token_limit, output_token_limit, attempt_limit,
                      reserved_cost_usd, measured_cost_usd, status
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (legacy_reservation,),
        ).fetchone() == (
            1_000_000,
            384_000,
            3,
            Decimal("2.500000"),
            Decimal("0.750000"),
            "review_required",
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0009_atomic_model_call_start",
        )
        assert connection.execute(
            "SELECT experiment_id FROM agent.model_calls WHERE id = %s",
            (model_call_id,),
        ).fetchone() == (explicit_experiment,)
        connection.execute("SAVEPOINT invalid_bound")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO agent.model_call_cost_reservations
                   (id, experiment_id, logical_call_id, reserved_cost_usd,
                    input_token_limit, output_token_limit, attempt_limit, status)
                   VALUES (%s, %s, %s, 0, 1, 1, 0, 'reserved')""",
                (uuid.uuid4(), explicit_experiment, uuid.uuid4()),
            )
        connection.execute("ROLLBACK TO SAVEPOINT invalid_bound")


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not DISPOSABLE_DATABASE,
    reason="disposable PostgreSQL owner URL not set",
)
def test_reservation_guard_migration_rejects_race_corrupted_state(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert OWNER_URL is not None
    config = _alembic_config()
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    terminal_reservation_id = uuid.uuid4()
    underfunded_reservation_id = uuid.uuid4()
    model_call_id = uuid.uuid4()
    logical_call_id = uuid.uuid4()

    command.downgrade(config, "0007_model_call_experiment")
    try:
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                """INSERT INTO agent.model_call_cost_reservations
                   (id, experiment_id, logical_call_id, reserved_cost_usd,
                    measured_cost_usd, input_token_limit, output_token_limit,
                    attempt_limit, status)
                   VALUES (%s, %s, %s, 0.000306, 0, 100, 1, 3, 'finalized')""",
                (
                    terminal_reservation_id,
                    reservation_test_state.experiment_id,
                    logical_call_id,
                ),
            )
            connection.execute(
                """INSERT INTO agent.model_calls
                   (id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
                    logical_call_id, operation, experiment_id, cost_reservation_id,
                    outcome, usage_available, cache_attribution_complete)
                   VALUES (%s, %s, %s, 'pageindex_generation', 'provider/model', 1,
                           %s, 'pageindex_generation', %s, %s, 'started', false, false)""",
                (
                    model_call_id,
                    reservation_test_state.attempt_id,
                    uuid.uuid4().hex,
                    logical_call_id,
                    reservation_test_state.experiment_id,
                    terminal_reservation_id,
                ),
            )
            connection.commit()

        with pytest.raises(
            ProgrammingError,
            match="started model call has terminal cost reservation",
        ):
            command.upgrade(config, "head")

        with psycopg.connect(owner_sync) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0007_model_call_experiment",
            )
            connection.execute(
                "DELETE FROM agent.model_calls WHERE id = %s",
                (model_call_id,),
            )
            connection.execute(
                "DELETE FROM agent.model_call_cost_reservations WHERE id = %s",
                (terminal_reservation_id,),
            )
            connection.execute(
                """INSERT INTO agent.model_call_cost_reservations
                   (id, experiment_id, logical_call_id, reserved_cost_usd,
                    input_token_limit, output_token_limit, attempt_limit, status)
                   VALUES (%s, %s, %s, 0.000001, 100, 1, 3, 'reserved')""",
                (
                    underfunded_reservation_id,
                    reservation_test_state.experiment_id,
                    uuid.uuid4(),
                ),
            )
            connection.commit()

        with pytest.raises(
            ProgrammingError,
            match="reserved model call cost reservation is underfunded",
        ):
            command.upgrade(config, "head")
    finally:
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                "DELETE FROM agent.model_calls WHERE id = %s",
                (model_call_id,),
            )
            connection.execute(
                """DELETE FROM agent.model_call_cost_reservations
                   WHERE id IN (%s, %s)""",
                (terminal_reservation_id, underfunded_reservation_id),
            )
            connection.commit()
        command.upgrade(config, "head")

    with psycopg.connect(owner_sync) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0009_atomic_model_call_start",
        )


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not DISPOSABLE_DATABASE,
    reason="disposable PostgreSQL owner URL not set",
)
def test_reservation_guard_migration_blocks_schema_0007_writer(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert OWNER_URL is not None
    config = _alembic_config()
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    model_call_id = uuid.uuid4()
    blocker = psycopg.connect(owner_sync)

    command.downgrade(config, "0007_model_call_experiment")
    try:
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                """INSERT INTO agent.model_calls
                   (id, query_run_id, trace_id, stage, model, provider_attempt,
                    logical_call_id, operation, outcome, usage_available,
                    cache_attribution_complete)
                   VALUES (%s, %s, %s, 'query_route', 'provider/model', 1, %s,
                           'query_route', 'started', false, false)""",
                (
                    model_call_id,
                    reservation_test_state.query_run_id,
                    uuid.uuid4().hex,
                    uuid.uuid4(),
                ),
            )
            connection.commit()

        blocker.execute("LOCK TABLE agent.experiments IN ACCESS EXCLUSIVE MODE")
        writer_started = threading.Event()

        def update_model_call() -> None:
            with psycopg.connect(owner_sync) as connection:
                writer_started.set()
                connection.execute(
                    "UPDATE agent.model_calls SET updated_at = now() WHERE id = %s",
                    (model_call_id,),
                )
                connection.commit()

        with ThreadPoolExecutor(max_workers=2) as executor:
            migration = executor.submit(command.upgrade, config, "head")
            try:
                deadline = time.monotonic() + 2
                with psycopg.connect(owner_sync, autocommit=True) as observer:
                    while time.monotonic() < deadline:
                        locks_held = observer.execute(
                            """SELECT EXISTS (
                                 SELECT 1
                                 FROM pg_locks calls
                                 WHERE calls.relation = 'agent.model_calls'::regclass
                                   AND calls.mode = 'ShareRowExclusiveLock'
                                   AND calls.granted
                                   AND EXISTS (
                                     SELECT 1
                                     FROM pg_locks reservations
                                     WHERE reservations.pid = calls.pid
                                       AND reservations.relation =
                                         'agent.model_call_cost_reservations'::regclass
                                       AND reservations.mode = 'ShareRowExclusiveLock'
                                       AND reservations.granted
                                   )
                                   AND EXISTS (
                                     SELECT 1
                                     FROM pg_locks experiments
                                     WHERE experiments.pid = calls.pid
                                       AND experiments.relation =
                                         'agent.experiments'::regclass
                                       AND experiments.mode = 'AccessShareLock'
                                       AND NOT experiments.granted
                                   )
                               )"""
                        ).fetchone()
                        if locks_held == (True,):
                            break
                        time.sleep(0.01)
                    else:
                        pytest.fail("migration did not hold both ledger table locks")

                writer = executor.submit(update_model_call)
                assert writer_started.wait(timeout=2)
                time.sleep(0.05)
                assert not writer.done()
            finally:
                blocker.rollback()

            migration.result(timeout=5)
            writer.result(timeout=5)
    finally:
        blocker.rollback()
        blocker.close()
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                "DELETE FROM agent.model_calls WHERE id = %s",
                (model_call_id,),
            )
            connection.commit()
        command.upgrade(config, "head")

    with psycopg.connect(owner_sync) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0009_atomic_model_call_start",
        )


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not DISPOSABLE_DATABASE,
    reason="disposable PostgreSQL owner URL not set",
)
def test_reservation_guard_migration_allows_inflight_schema_0007_finish(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert OWNER_URL is not None
    config = _alembic_config()
    separator = "&" if "?" in OWNER_URL else "?"
    config.set_main_option(
        "sqlalchemy.url",
        f"{OWNER_URL}{separator}application_name=reservation-guard-lock-retry-test",
    )
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    reservation_id = uuid.uuid4()
    model_call_id = uuid.uuid4()
    logical_call_id = uuid.uuid4()
    old_finish = psycopg.connect(owner_sync)

    command.downgrade(config, "0007_model_call_experiment")
    try:
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                """INSERT INTO agent.model_call_cost_reservations
                   (id, experiment_id, logical_call_id, reserved_cost_usd,
                    input_token_limit, output_token_limit, attempt_limit, status)
                   VALUES (%s, %s, %s, 0.000306, 100, 1, 3, 'reserved')""",
                (
                    reservation_id,
                    reservation_test_state.experiment_id,
                    logical_call_id,
                ),
            )
            connection.execute(
                """INSERT INTO agent.model_calls
                   (id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
                    logical_call_id, operation, experiment_id, cost_reservation_id,
                    outcome, usage_available, cache_attribution_complete)
                   VALUES (%s, %s, %s, 'pageindex_generation', 'provider/model', 1,
                           %s, 'pageindex_generation', %s, %s, 'started', false, false)""",
                (
                    model_call_id,
                    reservation_test_state.attempt_id,
                    uuid.uuid4().hex,
                    logical_call_id,
                    reservation_test_state.experiment_id,
                    reservation_id,
                ),
            )
            connection.commit()

        old_finish.execute("SET LOCAL lock_timeout = '2s'")
        old_finish.execute(
            """UPDATE agent.model_calls
               SET outcome = 'failed', failure_code = 'provider_rejected',
                   retryable = false, completed_at = now(), updated_at = now()
               WHERE id = %s""",
            (model_call_id,),
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            migration = executor.submit(command.upgrade, config, "head")
            try:
                deadline = time.monotonic() + 10
                with psycopg.connect(owner_sync, autocommit=True) as observer:
                    while time.monotonic() < deadline:
                        if migration.done():
                            migration.result()
                            pytest.fail(
                                "migration completed without entering the ledger lock retry"
                            )
                        lock_retry_active = observer.execute(
                            """SELECT EXISTS (
                                 SELECT 1
                                 FROM pg_stat_activity
                                 WHERE pid <> pg_backend_pid()
                                   AND application_name =
                                     'reservation-guard-lock-retry-test'
                                   AND state = 'active'
                                   AND (
                                     wait_event = 'PgSleep'
                                     OR wait_event_type = 'Lock'
                                   )
                               )"""
                        ).fetchone()
                        if lock_retry_active == (True,):
                            break
                        time.sleep(0.01)
                    else:
                        pytest.fail("migration did not enter the ledger lock retry")

                old_finish.execute(
                    """UPDATE agent.model_call_cost_reservations
                       SET status = 'review_required', updated_at = now()
                       WHERE id = %s""",
                    (reservation_id,),
                )
                old_finish.commit()
            finally:
                old_finish.rollback()

            migration.result(timeout=5)
    finally:
        old_finish.rollback()
        old_finish.close()
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                "DELETE FROM agent.model_calls WHERE id = %s",
                (model_call_id,),
            )
            connection.execute(
                "DELETE FROM agent.model_call_cost_reservations WHERE id = %s",
                (reservation_id,),
            )
            connection.commit()
        command.upgrade(config, "head")

    with psycopg.connect(owner_sync) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0009_atomic_model_call_start",
        )


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
def test_application_role_enforces_reservation_bounds_and_call_ownership(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None

    ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=1)
    with usage_context(
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        )
    ):
        with pytest.raises(ExperimentBudgetExceeded):
            ledger.start(
                ModelCallStage.pageindex_generation,
                "provider/model",
                1,
                input_token_limit=101,
                output_token_limit=1,
            )

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT count(*) FROM agent.model_calls
               WHERE ingestion_attempt_id = %s""",
            (reservation_test_state.attempt_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """SELECT count(*) FROM agent.model_call_cost_reservations
               WHERE experiment_id = %s""",
            (reservation_test_state.experiment_id,),
        ).fetchone() == (0,)

    with usage_context(
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        )
    ):
        call = ledger.start(
            ModelCallStage.pageindex_generation,
            "provider/model",
            1,
            input_token_limit=100,
            output_token_limit=1,
        )
    assert call is not None

    with usage_context(
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            query_run_id=reservation_test_state.query_run_id,
        )
    ):
        unreserved_call = ledger.start(
            ModelCallStage.query_route,
            "provider/model",
            1,
            input_token_limit=100,
            output_token_limit=1,
        )
    assert unreserved_call is not None

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT experiment_id, cost_reservation_id
               FROM agent.model_calls WHERE id = %s""",
            (call.id,),
        ).fetchone() == (
            reservation_test_state.experiment_id,
            call.cost_reservation_id,
        )
        assert connection.execute(
            """SELECT experiment_id, cost_reservation_id
               FROM agent.model_calls WHERE id = %s""",
            (unreserved_call.id,),
        ).fetchone() == (None, None)


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL or not DISPOSABLE_DATABASE,
    reason="disposable PostgreSQL owner and application URLs not set",
)
async def test_terminal_owner_recovery_abandons_only_interrupted_calls(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    other_experiment_id = uuid.UUID(int=reservation_test_state.experiment_id.int - 1)
    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        connection.execute(
            """INSERT INTO agent.experiment_memberships
               (id, experiment_id, query_run_id, trial_key, repetition,
                sequence_number, included)
               VALUES (%s, %s, %s, %s, 1, 0, false)""",
            (
                uuid.uuid4(),
                reservation_test_state.experiment_id,
                reservation_test_state.query_run_id,
                str(uuid.uuid4()),
            ),
        )
        connection.commit()

    with usage_context(
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            query_run_id=reservation_test_state.query_run_id,
            require_cost_reservation=True,
        )
    ):
        interrupted_call = await ledger.astart(
            ModelCallStage.query_document_select,
            "provider/model",
            1,
            input_token_limit=100,
            output_token_limit=1,
        )
    with usage_context(
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        )
    ):
        running_owner_call = await ledger.astart(
            ModelCallStage.pageindex_generation,
            "provider/model",
            1,
            input_token_limit=100,
            output_token_limit=1,
        )
    assert interrupted_call is not None
    assert running_owner_call is not None

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        _insert_reservation_test_experiment(
            connection,
            other_experiment_id,
            "other-recovery-experiment",
        )
        connection.execute(
            """INSERT INTO agent.experiment_memberships
               (id, experiment_id, query_run_id, trial_key, repetition,
                sequence_number, included)
               VALUES (%s, %s, %s, %s, 1, 0, false)""",
            (
                uuid.uuid4(),
                other_experiment_id,
                reservation_test_state.query_run_id,
                str(uuid.uuid4()),
            ),
        )
        connection.commit()

    with usage_context(
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            query_run_id=reservation_test_state.query_run_id,
            require_cost_reservation=True,
        )
    ):
        other_experiment_call = await ledger.astart(
            ModelCallStage.query_document_select,
            "provider/model",
            1,
            input_token_limit=100,
            output_token_limit=1,
        )
    assert other_experiment_call is not None

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        connection.execute(
            """UPDATE agent.query_runs
               SET error = 'execution_cancelled', failure_code = 'request_failed'
               WHERE id = %s""",
            (reservation_test_state.query_run_id,),
        )
        connection.commit()

    assert await ledger.arecover_terminal_owner_calls(reservation_test_state.experiment_id) == 1
    assert await ledger.arecover_terminal_owner_calls(reservation_test_state.experiment_id) == 0

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT c.outcome, c.failure_code, c.retryable, c.cancelled,
                      c.usage_available, c.completed_at IS NOT NULL,
                      r.status, r.reserved_cost_usd, r.measured_cost_usd
               FROM agent.model_calls c
               JOIN agent.model_call_cost_reservations r
                 ON r.id = c.cost_reservation_id
               WHERE c.id = %s""",
            (interrupted_call.id,),
        ).fetchone() == (
            "abandoned",
            "execution_interrupted",
            False,
            False,
            False,
            True,
            "review_required",
            Decimal("0.000102"),
            None,
        )
        assert connection.execute(
            """SELECT c.outcome, r.status
               FROM agent.model_calls c
               JOIN agent.model_call_cost_reservations r
                 ON r.id = c.cost_reservation_id
               WHERE c.id = %s""",
            (running_owner_call.id,),
        ).fetchone() == ("started", "reserved")
        assert connection.execute(
            """SELECT c.experiment_id, c.outcome, r.status
               FROM agent.model_calls c
               JOIN agent.model_call_cost_reservations r
                 ON r.id = c.cost_reservation_id
               WHERE c.id = %s""",
            (other_experiment_call.id,),
        ).fetchone() == (other_experiment_id, "started", "reserved")


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
async def test_cancelled_async_start_after_commit_finalizes_zero_cost_reservation(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    committed = asyncio.Event()
    release = asyncio.Event()
    cancellation_trace = uuid.uuid4().hex
    cancellation_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=1)
    cancellation_pool = cast(Any, cancellation_ledger)._async_pool
    cast(Any, cancellation_ledger)._async_pool = _PauseAfterCommitPool(
        cancellation_pool,
        committed,
        release,
    )

    async def start_at_commit_boundary() -> None:
        with usage_context(
            UsageOwner(
                trace_id=cancellation_trace,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            await cancellation_ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                1,
                input_token_limit=100,
                output_token_limit=1,
            )

    cancellation = asyncio.create_task(start_at_commit_boundary())
    await asyncio.wait_for(committed.wait(), timeout=2)
    cancellation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancellation, timeout=2)

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT c.outcome, c.cancelled, c.usage_available,
                      c.cache_attribution_complete, c.input_tokens, c.output_tokens,
                      r.status, r.measured_cost_usd
               FROM agent.model_calls c
               JOIN agent.model_call_cost_reservations r
                 ON r.id = c.cost_reservation_id
               WHERE c.trace_id = %s""",
            (cancellation_trace,),
        ).fetchone() == (
            "cancelled",
            True,
            True,
            True,
            0,
            0,
            "finalized",
            Decimal("0.000000"),
        )


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
async def test_cancelled_async_start_while_reservation_is_locked_rolls_back_promptly(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    executing = asyncio.Event()
    blocked_trace = uuid.uuid4().hex
    blocked_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=1)
    blocked_pool = cast(Any, blocked_ledger)._async_pool
    cast(Any, blocked_ledger)._async_pool = _SignalExecutePool(blocked_pool, executing)

    async def start_while_locked() -> None:
        with usage_context(
            UsageOwner(
                trace_id=blocked_trace,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            await blocked_ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                1,
                input_token_limit=100,
                output_token_limit=1,
            )

    with psycopg.connect(reservation_test_state.owner_sync) as blocker:
        blocker.execute(
            "SELECT id FROM agent.experiments WHERE id = %s FOR UPDATE",
            (reservation_test_state.experiment_id,),
        )
        blocked = asyncio.create_task(start_while_locked())
        await asyncio.wait_for(executing.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert not blocked.done()
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(blocked, timeout=1)

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agent.model_calls WHERE trace_id = %s",
            (blocked_trace,),
        ).fetchone() == (0,)
        assert connection.execute(
            """SELECT count(*) FROM agent.model_call_cost_reservations
               WHERE experiment_id = %s""",
            (reservation_test_state.experiment_id,),
        ).fetchone() == (0,)


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL or not DISPOSABLE_DATABASE,
    reason="disposable PostgreSQL owner and application URLs not set",
)
async def test_cancelled_atomic_start_rolls_back_server_side_reservation(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    advisory_lock_id = 8_675_309
    trace_id = uuid.uuid4().hex
    logical_call_id = uuid.uuid4()
    blocker = psycopg.connect(reservation_test_state.owner_sync)
    blocked: asyncio.Task[None] | None = None
    try:
        blocker.execute("SELECT pg_advisory_lock(%s)", (advisory_lock_id,))
        with psycopg.connect(reservation_test_state.owner_sync) as connection:
            connection.execute(
                """
                CREATE FUNCTION agent.test_pause_atomic_model_call_start()
                RETURNS trigger AS $$
                BEGIN
                  PERFORM pg_advisory_xact_lock(8675309);
                  RETURN NEW;
                END
                $$ LANGUAGE plpgsql;

                CREATE TRIGGER aaa_test_pause_atomic_model_call_start
                BEFORE INSERT ON agent.model_calls
                FOR EACH ROW EXECUTE FUNCTION agent.test_pause_atomic_model_call_start();
                """
            )
            connection.commit()

        ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=1)

        async def start_while_call_insert_is_blocked() -> None:
            with usage_context(
                UsageOwner(
                    trace_id=trace_id,
                    ingestion_attempt_id=reservation_test_state.attempt_id,
                )
            ):
                await ledger.astart(
                    ModelCallStage.pageindex_generation,
                    "provider/model",
                    1,
                    logical_call_id=logical_call_id,
                    input_token_limit=100,
                    output_token_limit=1,
                )

        blocked = asyncio.create_task(start_while_call_insert_is_blocked())
        with psycopg.connect(
            reservation_test_state.owner_sync,
            autocommit=True,
        ) as observer:
            for _ in range(200):
                waiting = observer.execute(
                    """SELECT EXISTS (
                         SELECT 1
                         FROM pg_locks
                         WHERE locktype = 'advisory'
                           AND NOT granted
                       )"""
                ).fetchone()
                if waiting == (True,):
                    break
                if blocked.done():
                    await blocked
                    pytest.fail("model-call start completed before the test lock was released")
                await asyncio.sleep(0.01)
            else:
                pytest.fail("model-call start did not block after reserving cost")

        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(blocked, timeout=2)

        with psycopg.connect(reservation_test_state.owner_sync) as connection:
            assert connection.execute(
                "SELECT count(*) FROM agent.model_calls WHERE trace_id = %s",
                (trace_id,),
            ).fetchone() == (0,)
            assert connection.execute(
                """SELECT count(*) FROM agent.model_call_cost_reservations
                   WHERE experiment_id = %s AND logical_call_id = %s""",
                (reservation_test_state.experiment_id, logical_call_id),
            ).fetchone() == (0,)
    finally:
        blocker.execute("SELECT pg_advisory_unlock(%s)", (advisory_lock_id,))
        blocker.close()
        if blocked is not None and not blocked.done():
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)
        with psycopg.connect(reservation_test_state.owner_sync) as connection:
            connection.execute(
                """DROP TRIGGER IF EXISTS aaa_test_pause_atomic_model_call_start
                   ON agent.model_calls;
                   DROP FUNCTION IF EXISTS agent.test_pause_atomic_model_call_start();
                """
            )
            connection.commit()


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
async def test_cancelled_retry_reconciles_existing_reservation(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    logical_call_id = uuid.uuid4()
    ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    first_call = await _create_retryable_reserved_call(
        ledger,
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        ),
        logical_call_id,
    )
    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT status FROM agent.model_call_cost_reservations
               WHERE id = %s""",
            (first_call.cost_reservation_id,),
        ).fetchone() == ("reserved",)

    blocked = asyncio.Event()
    release = asyncio.Event()
    retry_trace = uuid.uuid4().hex
    ledger_pool = cast(Any, ledger)._async_pool
    cast(Any, ledger)._async_pool = _PauseBeforeCallInsertPool(
        ledger_pool,
        blocked,
        release,
    )

    async def start_retry() -> None:
        with usage_context(
            UsageOwner(
                trace_id=retry_trace,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            await ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                2,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    retry = asyncio.create_task(start_retry())
    await asyncio.wait_for(blocked.wait(), timeout=2)
    retry.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(retry, timeout=2)

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT count(*) FROM agent.model_calls
               WHERE logical_call_id = %s""",
            (logical_call_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """SELECT status FROM agent.model_call_cost_reservations
               WHERE id = %s""",
            (first_call.cost_reservation_id,),
        ).fetchone() == ("review_required",)


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
async def test_retry_start_serializes_with_cancellation_cleanup(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    logical_call_id = uuid.uuid4()
    cancelled_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    first_call = await _create_retryable_reserved_call(
        cancelled_ledger,
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        ),
        logical_call_id,
    )
    insert_blocked = asyncio.Event()
    insert_release = asyncio.Event()
    cleanup_locked = asyncio.Event()
    cleanup_release = asyncio.Event()
    cancelled_pool = cast(Any, cancelled_ledger)._async_pool
    cast(Any, cancelled_ledger)._async_pool = _PauseCleanupAfterReservationPool(
        cancelled_pool,
        insert_blocked,
        insert_release,
        cleanup_locked,
        cleanup_release,
    )

    async def start_cancelled_retry() -> None:
        with usage_context(
            UsageOwner(
                trace_id=uuid.uuid4().hex,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            await cancelled_ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                2,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    cancelled_retry = asyncio.create_task(start_cancelled_retry())
    await asyncio.wait_for(insert_blocked.wait(), timeout=2)
    cancelled_retry.cancel()
    await asyncio.wait_for(cleanup_locked.wait(), timeout=2)

    competing_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)

    async def start_competing_retry() -> None:
        with usage_context(
            UsageOwner(
                trace_id=uuid.uuid4().hex,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            await competing_ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                2,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    competing_retry = asyncio.create_task(start_competing_retry())
    try:
        await asyncio.sleep(0.05)
        assert not competing_retry.done()
    finally:
        cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled_retry, timeout=2)
    with pytest.raises(UsagePersistenceError):
        await asyncio.wait_for(competing_retry, timeout=2)

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT count(*) FROM agent.model_calls
               WHERE logical_call_id = %s""",
            (logical_call_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """SELECT status FROM agent.model_call_cost_reservations
               WHERE id = %s""",
            (first_call.cost_reservation_id,),
        ).fetchone() == ("review_required",)


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
async def test_cancellation_cleanup_preserves_competing_started_retry(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    logical_call_id = uuid.uuid4()
    competing_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    first_call = await _create_retryable_reserved_call(
        competing_ledger,
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        ),
        logical_call_id,
    )
    competitor_inserted = asyncio.Event()
    competitor_release = asyncio.Event()
    competing_pool = cast(Any, competing_ledger)._async_pool
    cast(Any, competing_ledger)._async_pool = _PauseAfterCallInsertPool(
        competing_pool,
        competitor_inserted,
        competitor_release,
    )

    async def start_competing_retry() -> StartedModelCall | None:
        with usage_context(
            UsageOwner(
                trace_id=uuid.uuid4().hex,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            return await competing_ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                2,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    competing_retry = asyncio.create_task(start_competing_retry())
    await asyncio.wait_for(competitor_inserted.wait(), timeout=2)

    cancelled_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    reservation_started = asyncio.Event()
    cancelled_pool = cast(Any, cancelled_ledger)._async_pool
    cast(Any, cancelled_ledger)._async_pool = _SignalExecutePool(
        cancelled_pool,
        reservation_started,
    )

    async def start_cancelled_retry() -> None:
        with usage_context(
            UsageOwner(
                trace_id=uuid.uuid4().hex,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            await cancelled_ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                3,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    cancelled_retry = asyncio.create_task(start_cancelled_retry())
    await asyncio.wait_for(reservation_started.wait(), timeout=2)
    await asyncio.sleep(0.05)
    assert not cancelled_retry.done()
    cancelled_retry.cancel()
    await asyncio.sleep(0.05)
    assert not cancelled_retry.done()

    competitor_release.set()
    competing_call = await asyncio.wait_for(competing_retry, timeout=2)
    assert competing_call is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled_retry, timeout=2)

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT count(*) FROM agent.model_calls
               WHERE logical_call_id = %s AND outcome = 'started'""",
            (logical_call_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """SELECT status FROM agent.model_call_cost_reservations
               WHERE id = %s""",
            (first_call.cost_reservation_id,),
        ).fetchone() == ("reserved",)

    await competing_ledger.afinish(
        competing_call,
        ModelCallOutcome.failed,
        None,
        TokenUsage.unavailable(),
        failure_code="provider_rejected",
        retryable=False,
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
async def test_terminal_finish_preserves_competing_started_retry(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    logical_call_id = uuid.uuid4()
    finishing_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    first_call = await _create_retryable_reserved_call(
        finishing_ledger,
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        ),
        logical_call_id,
    )
    with usage_context(
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        )
    ):
        finishing_call = await finishing_ledger.astart(
            ModelCallStage.pageindex_generation,
            "provider/model",
            2,
            logical_call_id=logical_call_id,
            input_token_limit=100,
            output_token_limit=1,
        )
    assert finishing_call is not None

    competitor_inserted = asyncio.Event()
    competitor_release = asyncio.Event()
    competing_ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    competing_pool = cast(Any, competing_ledger)._async_pool
    cast(Any, competing_ledger)._async_pool = _PauseAfterCallInsertPool(
        competing_pool,
        competitor_inserted,
        competitor_release,
    )

    async def start_competing_retry() -> StartedModelCall | None:
        with usage_context(
            UsageOwner(
                trace_id=uuid.uuid4().hex,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            return await competing_ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                3,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    competing_retry = asyncio.create_task(start_competing_retry())
    await asyncio.wait_for(competitor_inserted.wait(), timeout=2)
    finishing = asyncio.create_task(
        finishing_ledger.afinish(
            finishing_call,
            ModelCallOutcome.failed,
            None,
            TokenUsage.unavailable(),
            failure_code="provider_rejected",
            retryable=False,
        )
    )
    await asyncio.sleep(0.05)
    assert not finishing.done()

    competitor_release.set()
    competing_call = await asyncio.wait_for(competing_retry, timeout=2)
    assert competing_call is not None
    await asyncio.wait_for(finishing, timeout=2)

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT count(*) FROM agent.model_calls
               WHERE logical_call_id = %s AND outcome = 'started'""",
            (logical_call_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """SELECT status FROM agent.model_call_cost_reservations
               WHERE id = %s""",
            (first_call.cost_reservation_id,),
        ).fetchone() == ("reserved",)

    await competing_ledger.afinish(
        competing_call,
        ModelCallOutcome.failed,
        None,
        TokenUsage.unavailable(),
        failure_code="provider_rejected",
        retryable=False,
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
async def test_reservation_state_trigger_rejects_rolling_old_reconciler(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    logical_call_id = uuid.uuid4()
    ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    first_call = await _create_retryable_reserved_call(
        ledger,
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            ingestion_attempt_id=reservation_test_state.attempt_id,
        ),
        logical_call_id,
    )
    competitor_inserted = asyncio.Event()
    competitor_release = asyncio.Event()
    ledger_pool = cast(Any, ledger)._async_pool
    cast(Any, ledger)._async_pool = _PauseAfterCallInsertPool(
        ledger_pool,
        competitor_inserted,
        competitor_release,
    )

    async def start_competing_retry() -> StartedModelCall | None:
        with usage_context(
            UsageOwner(
                trace_id=uuid.uuid4().hex,
                ingestion_attempt_id=reservation_test_state.attempt_id,
            )
        ):
            return await ledger.astart(
                ModelCallStage.pageindex_generation,
                "provider/model",
                2,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    competing_retry = asyncio.create_task(start_competing_retry())
    try:
        await asyncio.wait_for(competitor_inserted.wait(), timeout=2)
        reconciliation_started = threading.Event()

        def reconcile_without_reservation_lock() -> None:
            with psycopg.connect(reservation_test_state.owner_sync) as connection:
                reconciliation_started.set()
                connection.execute(
                    """UPDATE agent.model_call_cost_reservations
                       SET measured_cost_usd = 0,
                           reserved_cost_usd = 0,
                           status = 'finalized',
                           updated_at = now()
                       WHERE id = %s""",
                    (first_call.cost_reservation_id,),
                )
                connection.commit()

        with ThreadPoolExecutor(max_workers=1) as executor:
            old_reconciliation = executor.submit(reconcile_without_reservation_lock)
            try:
                assert await asyncio.to_thread(reconciliation_started.wait, 2)
                await asyncio.sleep(0.05)
                assert not old_reconciliation.done()
            finally:
                competitor_release.set()
                competing_result = (await asyncio.gather(competing_retry, return_exceptions=True))[
                    0
                ]

            assert not isinstance(competing_result, BaseException)
            competing_call = competing_result
            assert competing_call is not None
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="terminal cost reservation conflicts with a started model call",
            ):
                await asyncio.wrap_future(old_reconciliation)

        with psycopg.connect(reservation_test_state.owner_sync) as connection:
            assert connection.execute(
                """SELECT status FROM agent.model_call_cost_reservations
                   WHERE id = %s""",
                (first_call.cost_reservation_id,),
            ).fetchone() == ("reserved",)

        await ledger.afinish(
            competing_call,
            ModelCallOutcome.failed,
            None,
            TokenUsage.unavailable(),
            failure_code="provider_rejected",
            retryable=False,
        )

        with psycopg.connect(reservation_test_state.owner_sync) as connection:
            connection.execute(
                """UPDATE agent.model_call_cost_reservations
                   SET reserved_cost_usd = 0, status = 'reserved'
                   WHERE id = %s""",
                (first_call.cost_reservation_id,),
            )
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="reserved model call cost reservation is underfunded",
            ):
                connection.commit()
            connection.rollback()
    finally:
        competitor_release.set()
        if not competing_retry.done():
            competing_retry.cancel()
        await asyncio.gather(competing_retry, return_exceptions=True)


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL or not DISPOSABLE_DATABASE,
    reason="disposable PostgreSQL owner and application URLs are required",
)
async def test_query_owner_cleanup_uses_allocator_experiment_order(
    reservation_test_state: _ReservationTestState,
) -> None:
    assert APP_URL is not None
    second_experiment_id = uuid.uuid4()
    experiment_ids = sorted(
        (reservation_test_state.experiment_id, second_experiment_id),
        key=lambda value: value.int,
    )
    allocated_experiment_id, other_experiment_id = experiment_ids
    logical_call_id = uuid.uuid4()
    other_reservation_id = uuid.uuid4()
    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        _insert_reservation_test_experiment(
            connection,
            second_experiment_id,
            "second-query-membership",
        )
        connection.execute(
            """INSERT INTO agent.experiment_memberships
               (id, experiment_id, query_run_id, trial_key, repetition,
                sequence_number, included)
               VALUES (%s, %s, %s, %s, 1, 0, true),
                      (%s, %s, %s, %s, 1, 1, true)""",
            (
                uuid.uuid4(),
                reservation_test_state.experiment_id,
                reservation_test_state.query_run_id,
                uuid.uuid4().hex,
                uuid.uuid4(),
                second_experiment_id,
                reservation_test_state.query_run_id,
                uuid.uuid4().hex,
            ),
        )
        connection.commit()

    ledger = ModelCallLedger(APP_URL, maximum_provider_attempts=3)
    first_call = await _create_retryable_reserved_call(
        ledger,
        UsageOwner(
            trace_id=uuid.uuid4().hex,
            query_run_id=reservation_test_state.query_run_id,
        ),
        logical_call_id,
        stage=ModelCallStage.query_route,
    )
    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        assert connection.execute(
            """SELECT experiment_id FROM agent.model_call_cost_reservations
               WHERE id = %s""",
            (first_call.cost_reservation_id,),
        ).fetchone() == (allocated_experiment_id,)
        connection.execute(
            """INSERT INTO agent.model_call_cost_reservations
               (id, experiment_id, logical_call_id, reserved_cost_usd,
                input_token_limit, output_token_limit, attempt_limit, status)
               VALUES (%s, %s, %s, 1, 100, 1, 3, 'reserved')""",
            (other_reservation_id, other_experiment_id, logical_call_id),
        )
        connection.commit()

    blocked = asyncio.Event()
    release = asyncio.Event()
    ledger_pool = cast(Any, ledger)._async_pool
    cast(Any, ledger)._async_pool = _PauseBeforeCallInsertPool(
        ledger_pool,
        blocked,
        release,
    )

    async def start_retry() -> None:
        with usage_context(
            UsageOwner(
                trace_id=uuid.uuid4().hex,
                query_run_id=reservation_test_state.query_run_id,
            )
        ):
            await ledger.astart(
                ModelCallStage.query_route,
                "provider/model",
                2,
                logical_call_id=logical_call_id,
                input_token_limit=100,
                output_token_limit=1,
            )

    retry = asyncio.create_task(start_retry())
    await asyncio.wait_for(blocked.wait(), timeout=2)
    retry.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(retry, timeout=2)

    with psycopg.connect(reservation_test_state.owner_sync) as connection:
        statuses = dict(
            connection.execute(
                """SELECT experiment_id, status
                   FROM agent.model_call_cost_reservations
                   WHERE logical_call_id = %s""",
                (logical_call_id,),
            ).fetchall()
        )
    assert statuses == {
        allocated_experiment_id: "review_required",
        other_experiment_id: "reserved",
    }
    # Memberships are append-only; the disposable stack owns their teardown.


class _UnusedRouterLLM:
    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del schema, system, user, thinking, stage, max_output_tokens
        raise AssertionError("candidate lookup does not invoke the LLM")


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
async def test_classification_report_queries_migrated_view_contract() -> None:
    assert OWNER_URL is not None

    report = await classification_report(
        Settings(
            database_admin_url=OWNER_URL,
            langfuse_public_key=None,
            langfuse_secret_key=None,
        )
    )

    effective = cast(dict[str, int], report["effective"])
    assert report["apply"] is False
    assert set(effective) == {"excluded", "reinstated"}
    assert effective["excluded"] >= 0
    assert effective["reinstated"] >= 0


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
async def test_provider_admission_requires_the_retry_cooldown() -> None:
    assert OWNER_URL is not None
    engine = make_engine(OWNER_URL, pool_size=2)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    document = Document(
        id=uuid.uuid4(),
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_filename="provider-cooldown.pdf",
        pdf_object_key="pdf/provider-cooldown.pdf",
        status=DocumentStatus.waiting_provider,
        diagnostic="waiting",
    )
    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status=JobStatus.waiting_provider,
        provider_wait_reason="pageindex_rate_limited",
        current_stage="waiting_provider",
        is_active=True,
    )
    try:
        async with sessions() as session:
            session.add_all([document, job])
            await session.commit()
            await session.refresh(job)
            failed_at = job.updated_at

        async with sessions() as session:
            await set_ingestion_provider_admission(
                session,
                available=True,
                reason=None,
                checked_at=failed_at + timedelta(seconds=1),
            )
            await session.commit()
            waiting = await session.get(IngestionJob, job.id)
            assert waiting is not None
            assert waiting.status == JobStatus.waiting_provider

        async with sessions() as session:
            await set_ingestion_provider_admission(
                session,
                available=True,
                reason=None,
                checked_at=failed_at + timedelta(seconds=31),
            )
            await session.commit()
            queued = await session.get(IngestionJob, job.id)
            available_document = await session.get(Document, document.id)
            assert queued is not None and queued.status == JobStatus.queued
            assert queued.provider_wait_reason is None
            assert available_document is not None
            assert available_document.status == DocumentStatus.queued
            assert available_document.diagnostic is None
    finally:
        async with sessions() as session:
            await session.execute(delete(IngestionJob).where(IngestionJob.id == job.id))
            await session.execute(delete(Document).where(Document.id == document.id))
            await session.commit()
        await engine.dispose()


def _router_document(
    label: str,
    *,
    title: str,
    abstract: str,
    authors: list[str] | None = None,
    topics: list[str] | None = None,
    submitted_date: date | None = None,
    status: DocumentStatus = DocumentStatus.ready,
    created_at: datetime,
) -> Document:
    identifier = uuid.uuid4()
    return Document(
        id=identifier,
        sha256=identifier.hex + identifier.hex,
        arxiv_id=f"r-{identifier.hex[:10]}",
        original_filename=f"router-{label}.pdf",
        pdf_object_key=f"pdf/router-{identifier}.pdf",
        title=title,
        abstract=abstract,
        authors=authors or [],
        topics=topics or [],
        submitted_date=submitted_date,
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL or not READER_URL, reason="PostgreSQL integration URLs not set")
async def test_migration_search_reader_permissions_and_guarded_execution() -> None:
    assert OWNER_URL is not None
    assert READER_URL is not None
    identifier = uuid.uuid4()
    digest = identifier.hex + identifier.hex
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    reader_sync = READER_URL.replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(owner_sync, autocommit=True) as connection:
            privileges = connection.execute(
                """SELECT
                     has_table_privilege('rag_app', 'agent.ingestion_attempts', 'INSERT'),
                     has_table_privilege('rag_app', 'agent.model_calls', 'INSERT'),
                     has_table_privilege('rag_app', 'agent.model_calls', 'UPDATE'),
                     has_table_privilege('rag_reader', 'agent.ingestion_attempts', 'SELECT'),
                     has_table_privilege('rag_reader', 'agent.model_calls', 'SELECT')"""
            ).fetchone()
            assert privileges == (True, True, True, False, False)
            connection.execute(
                """INSERT INTO agent.documents
                   (id, sha256, original_filename, pdf_object_key, title, abstract, topics, status)
                   VALUES (%s, %s, 'test.pdf', 'pdf/test', 'Vectorless Retrieval',
                           'Text to image reasoning based search',
                           ARRAY['rag', 'pageindex'], 'ready')""",
                (identifier, digest),
            )
        with psycopg.connect(reader_sync, autocommit=True) as connection:
            found = connection.execute(
                """SELECT document_id FROM agent.paper_catalog_v1
                   WHERE document_id = %s""",
                (identifier,),
            ).fetchone()
            assert found == (identifier,)
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM agent.documents").fetchall()
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute("DELETE FROM agent.paper_catalog_v1")
            regex_modes = connection.execute(
                """SELECT
                     'AI' ~ '(?b)AI',
                     'AI' ~ '(?e)AI',
                     'AI' ~ '(?q)AI',
                     'AI' ~ '***:(?im)^ai$',
                     'AI' ~ '***=AI'"""
            ).fetchone()
            assert regex_modes == (True, True, True, True, True)
        settings = Settings(sql_database_url=READER_URL)
        engine = make_engine(READER_URL, pool_size=2)
        try:
            result = await PostgreSQLAdapter(engine, settings).execute(
                validate_sql(
                    "SELECT document_id, title FROM agent.paper_catalog_v1 "
                    "WHERE title = 'Vectorless Retrieval'"
                )
            )
            assert result.rows == [{"document_id": identifier, "title": "Vectorless Retrieval"}]
            assert result.explain_cost >= 0
            colon_literal = await PostgreSQLAdapter(engine, settings).execute(
                validate_sql(
                    "SELECT document_id, title FROM agent.paper_catalog_v1 "
                    r"WHERE title ~* '\y(?:large\s+)?vectorless\y'"
                )
            )
            assert colon_literal.rows == [
                {"document_id": identifier, "title": "Vectorless Retrieval"}
            ]
            percent_literal = await PostgreSQLAdapter(engine, settings).execute(
                validate_sql(
                    "SELECT document_id, title FROM agent.paper_catalog_v1 "
                    "WHERE title ILIKE '%Vectorless%'"
                )
            )
            assert percent_literal.rows == [
                {"document_id": identifier, "title": "Vectorless Retrieval"}
            ]
            bounded_short_term = await PostgreSQLAdapter(engine, settings).execute(
                validate_sql(
                    _normalize_short_regex_terms(
                        "SELECT document_id, title FROM agent.paper_catalog_v1 "
                        r"WHERE abstract ~* 'text[-\s]+to[-\s]+image'"
                    )
                )
            )
            assert bounded_short_term.rows == [
                {"document_id": identifier, "title": "Vectorless Retrieval"}
            ]
            for unsafe_sql in (
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '(?b)\mai\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '(?e)\mai\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '(?q)\mai\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '***=\mai\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '(?b)\\mai\\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '(?e)\\mai\\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '(?q)\\mai\\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '***=\\mai\\M'",
                r"SELECT document_id, title FROM agent.paper_catalog_v1 "
                r"WHERE title ~* '***:(?q)\\mai\\M'",
            ):
                assert _normalize_short_regex_terms(unsafe_sql) == unsafe_sql
                with pytest.raises(SQLRejected, match="cannot use ARE word boundaries"):
                    validate_sql(unsafe_sql)
            embedded_options = await PostgreSQLAdapter(engine, settings).execute(
                validate_sql(
                    _normalize_short_regex_terms(
                        "SELECT document_id, title FROM agent.paper_catalog_v1 "
                        r"WHERE title = 'Vectorless Retrieval' "
                        r"AND 'AI' ~ '***:(?im)^ai$'"
                    )
                )
            )
            assert embedded_options.rows == [
                {"document_id": identifier, "title": "Vectorless Retrieval"}
            ]
        finally:
            await engine.dispose()
    finally:
        with psycopg.connect(owner_sync, autocommit=True) as connection:
            connection.execute("DELETE FROM agent.documents WHERE id = %s", (identifier,))


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
async def test_corpus_candidates_match_then_fill_ordering_and_filters() -> None:
    assert OWNER_URL is not None
    engine = make_engine(OWNER_URL, pool_size=2)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    unique = uuid.uuid4().hex
    precision_alpha = f"precision{unique}"
    precision_beta = f"alpha{unique}"
    nebula = f"nebula{unique}"
    absent = f"absent{unique}"
    scoped_alpha = f"scoped{unique}"
    scoped_beta = f"exact{unique}"
    quoted_alpha = f"quoted{unique}"
    quoted_beta = f"cobalt{unique}"
    documents = [
        _router_document(
            "precision-match",
            title=f"{precision_alpha} {precision_beta}",
            abstract="exact precision match",
            created_at=now - timedelta(seconds=12),
        ),
        _router_document(
            "precision-fill",
            title="precision fill",
            abstract="no shared query vocabulary",
            created_at=now - timedelta(seconds=11),
        ),
        _router_document(
            "precision-other",
            title="precision other",
            abstract="another unrelated record",
            created_at=now - timedelta(seconds=10),
        ),
        _router_document(
            "nebula-ranked",
            title="ranked fill",
            abstract=f"contains {nebula} for OR ranking",
            created_at=now - timedelta(seconds=15),
        ),
        _router_document(
            "nebula-newer-unrelated",
            title="newer unrelated fill",
            abstract="no overlap here",
            created_at=now - timedelta(seconds=1),
        ),
        _router_document(
            "nebula-older-unrelated",
            title="older unrelated fill",
            abstract="still no overlap",
            created_at=now - timedelta(seconds=20),
        ),
        _router_document(
            "scoped-match",
            title=f"{scoped_alpha} {scoped_beta}",
            abstract="allowed exact result",
            authors=["Allowed Author"],
            topics=["allowed-topic"],
            submitted_date=date(2026, 1, 2),
            created_at=now - timedelta(seconds=8),
        ),
        _router_document(
            "scoped-fill",
            title="allowed fill result",
            abstract="not a lexical match",
            authors=["Allowed Author"],
            topics=["allowed-topic"],
            submitted_date=date(2026, 1, 3),
            created_at=now - timedelta(seconds=7),
        ),
        _router_document(
            "scoped-wrong-author",
            title=f"{scoped_alpha} {scoped_beta}",
            abstract="must fail metadata constraints",
            authors=["Different Author"],
            topics=["allowed-topic"],
            submitted_date=date(2026, 1, 4),
            created_at=now - timedelta(seconds=6),
        ),
        _router_document(
            "scoped-not-ready",
            title="allowed-looking but unavailable",
            abstract="must fail ready filter",
            authors=["Allowed Author"],
            topics=["allowed-topic"],
            submitted_date=date(2026, 1, 5),
            status=DocumentStatus.failed,
            created_at=now - timedelta(seconds=5),
        ),
        _router_document(
            "quoted-match",
            title=f"{quoted_alpha} {quoted_beta}",
            abstract="matches only the quoted phrase",
            created_at=now - timedelta(seconds=4),
        ),
        _router_document(
            "quoted-fill",
            title="quoted query fill",
            abstract="unrelated fill record",
            created_at=now - timedelta(seconds=3),
        ),
    ]
    document_ids = [document.id for document in documents]
    try:
        async with sessions() as session:
            session.add_all(documents)
            await session.commit()
            versions = [
                IndexVersion(
                    id=uuid.uuid4(),
                    document_id=document.id,
                    version_key=f"integration-{document.id}",
                    pageindex_version="integration",
                    prompt_version="integration",
                    model_name="integration",
                    artifact_object_key=f"artifacts/{document.id}.json",
                    artifact_sha256=document.sha256,
                    artifact_schema_version=1,
                    recipe_key=f"integration-{document.id}",
                    pageindex_description=document.abstract,
                    lifecycle_status=IndexLifecycleStatus.verified,
                    verified_at=now,
                )
                for document in documents
                if document.status == DocumentStatus.ready
            ]
            session.add_all(versions)
            await session.commit()
            versions_by_document = {version.document_id: version for version in versions}
            for document in documents:
                version = versions_by_document.get(document.id)
                if version is not None:
                    document.active_index_version_id = version.id
            await session.commit()
            router = CorpusRouter(Settings(candidate_limit=3), _UnusedRouterLLM())

            precision_ids = [document.id for document in documents[:3]]
            precision = await router.candidates(
                session,
                f"{precision_alpha} {precision_beta}",
                MetadataConstraints(),
                precision_ids,
            )
            assert precision[0].document.id == documents[0].id
            assert {candidate.document.id for candidate in precision[1:]} == {
                documents[1].id,
                documents[2].id,
            }
            assert [candidate.fts_match for candidate in precision] == [True, False, False]
            assert len({candidate.document.id for candidate in precision}) == 3

            nebula_documents = documents[3:6]
            zero_and_match = await router.candidates(
                session,
                f"{nebula} {absent}",
                MetadataConstraints(),
                [document.id for document in nebula_documents],
            )
            assert len(zero_and_match) == 3
            assert all(not candidate.fts_match for candidate in zero_and_match)
            assert zero_and_match[0].document.id == documents[3].id
            assert {candidate.document.id for candidate in zero_and_match} == {
                document.id for document in nebula_documents
            }

            constrained_documents = documents[6:10]
            constrained = await router.candidates(
                session,
                f"{scoped_alpha} {scoped_beta}",
                MetadataConstraints(
                    authors=["Allowed Author"],
                    topics=["allowed-topic"],
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                ),
                [document.id for document in constrained_documents],
            )
            assert [candidate.document.id for candidate in constrained] == [
                documents[6].id,
                documents[7].id,
            ]
            assert [candidate.fts_match for candidate in constrained] == [True, False]

            quoted = await router.candidates(
                session,
                f'Which paper contains "{quoted_alpha} {quoted_beta}" plus {absent}?',
                MetadataConstraints(),
                [documents[10].id, documents[11].id],
            )
            assert [candidate.document.id for candidate in quoted] == [
                documents[10].id,
                documents[11].id,
            ]
            assert [candidate.fts_match for candidate in quoted] == [True, False]
    finally:
        async with sessions() as session:
            await session.execute(
                update(Document)
                .where(Document.id.in_(document_ids))
                .values(active_index_version_id=None)
            )
            await session.execute(
                delete(IndexVersion).where(IndexVersion.document_id.in_(document_ids))
            )
            await session.execute(delete(Document).where(Document.id.in_(document_ids)))
            await session.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
async def test_concurrent_claims_reclaim_stale_work_and_exhaust_recovery() -> None:
    assert OWNER_URL is not None
    engine = make_engine(OWNER_URL, pool_size=4)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    document_ids = [uuid.uuid4() for _ in range(7)]
    now = datetime.now(UTC)
    created_baseline = now - timedelta(days=3_650)
    jobs = [
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[0],
            status=JobStatus.queued,
            attempts=0,
            created_at=created_baseline - timedelta(seconds=7),
        ),
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[1],
            status=JobStatus.queued,
            attempts=0,
            created_at=created_baseline - timedelta(seconds=6),
        ),
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[2],
            status=JobStatus.running,
            attempts=1,
            retry_budget_consumed=1,
            started_at=now - timedelta(seconds=120),
            created_at=created_baseline - timedelta(seconds=5),
        ),
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[3],
            status=JobStatus.running,
            attempts=3,
            retry_budget_consumed=3,
            started_at=now - timedelta(seconds=120),
            created_at=created_baseline - timedelta(seconds=4),
        ),
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[4],
            status=JobStatus.running,
            attempts=1,
            retry_budget_consumed=1,
            started_at=now,
            created_at=created_baseline - timedelta(seconds=3),
        ),
    ]
    settings = Settings(
        database_url=OWNER_URL,
        pageindex_timeout_seconds=60,
        ingestion_stale_grace_seconds=10,
        ingestion_max_attempts=3,
    )
    try:
        async with sessions() as session:
            session.add_all(
                [
                    Document(
                        id=document_id,
                        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                        original_filename=f"claim-test-{index}.pdf",
                        pdf_object_key=f"pdf/{document_id}.pdf",
                        status=DocumentStatus.queued,
                    )
                    for index, document_id in enumerate(document_ids)
                ]
            )
            session.add_all(jobs)
            session.add_all(
                [
                    IngestionAttempt(
                        id=uuid.uuid4(),
                        job_id=job.id,
                        attempt_number=job.attempts,
                        trace_id=uuid.uuid4().hex,
                        worker_id=job.worker_id or f"prior-worker-{index}",
                        status=IngestionAttemptStatus.running,
                        started_at=job.started_at or now,
                    )
                    for index, job in enumerate(jobs[2:5], 2)
                ]
            )
            await session.commit()

        async with sessions() as first, sessions() as second:
            first_claim = await claim_ingestion_job(first, "worker-1", settings)
            second_claim = await claim_ingestion_job(second, "worker-2", settings)
            assert first_claim is not None and first_claim.job_id == jobs[0].id
            assert second_claim is not None and second_claim.job_id == jobs[1].id
            assert first_claim.attempt_number == 1
            assert second_claim.attempt_number == 1
            await first.commit()
            await second.commit()

        async with sessions() as session:
            stale_claim = await claim_ingestion_job(session, "worker-3", settings)
            assert stale_claim is not None and stale_claim.job_id == jobs[2].id
            assert stale_claim.attempt_number == 2
            stale_attempts = list(
                (
                    await session.scalars(
                        select(IngestionAttempt)
                        .where(IngestionAttempt.job_id == jobs[2].id)
                        .order_by(IngestionAttempt.attempt_number)
                    )
                ).all()
            )
            assert [attempt.status for attempt in stale_attempts] == [
                IngestionAttemptStatus.abandoned,
                IngestionAttemptStatus.running,
            ]
            await session.commit()

        async with sessions() as session:
            exhausted = await claim_ingestion_job(session, "worker-4", settings)
            assert exhausted is None
            await session.commit()
            failed_job = await session.get(IngestionJob, jobs[3].id)
            document = await session.get(Document, document_ids[3])
            assert failed_job is not None
            assert failed_job.status == JobStatus.failed
            assert failed_job.metrics["failure_code"] == "ingestion_recovery_exhausted"
            assert document is not None and document.status == DocumentStatus.failed
            exhausted_attempt = await session.scalar(
                select(IngestionAttempt).where(IngestionAttempt.job_id == jobs[3].id)
            )
            assert exhausted_attempt is not None
            assert exhausted_attempt.status == IngestionAttemptStatus.failed

        locked_job = IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[5],
            status=JobStatus.running,
            attempts=1,
            retry_budget_consumed=1,
            started_at=now - timedelta(seconds=120),
            created_at=created_baseline - timedelta(seconds=2),
        )
        fallback_job = IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[6],
            status=JobStatus.queued,
            attempts=0,
            created_at=created_baseline - timedelta(seconds=1),
        )
        async with sessions() as session:
            session.add_all([locked_job, fallback_job])
            await session.commit()

        async with sessions() as locking, sessions() as claiming:
            await locking.scalar(
                select(IngestionJob).where(IngestionJob.id == locked_job.id).with_for_update()
            )
            skipped_claim = await claim_ingestion_job(claiming, "worker-5", settings)
            assert skipped_claim is not None and skipped_claim.job_id == fallback_job.id
            await claiming.commit()
            await locking.rollback()

        async with sessions() as session:
            live_job = await session.get(IngestionJob, jobs[4].id)
            still_locked_job = await session.get(IngestionJob, locked_job.id)
            assert live_job is not None and live_job.status == JobStatus.running
            assert live_job.attempts == 1
            assert still_locked_job is not None and still_locked_job.attempts == 1
    finally:
        async with sessions() as session:
            await session.execute(
                update(IngestionJob)
                .where(IngestionJob.document_id.in_(document_ids))
                .values(active_attempt_id=None)
            )
            await session.execute(
                delete(IngestionAttempt).where(
                    IngestionAttempt.job_id.in_(
                        select(IngestionJob.id).where(IngestionJob.document_id.in_(document_ids))
                    )
                )
            )
            await session.execute(
                delete(IngestionJob).where(IngestionJob.document_id.in_(document_ids))
            )
            await session.execute(delete(Document).where(Document.id.in_(document_ids)))
            await session.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
def test_experiment_reservations_enforce_cap_and_reconcile_measured_cost() -> None:
    assert OWNER_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    experiment_id = uuid.uuid4()
    logical_call_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    with psycopg.connect(owner_sync) as connection:
        connection.execute(
            """INSERT INTO agent.documents
               (id, sha256, original_filename, pdf_object_key, status)
               VALUES (%s, %s, 'budget.pdf', 'pdf/budget', 'indexing')""",
            (document_id, digest),
        )
        connection.execute(
            """INSERT INTO agent.experiments
               (id, name, release_sha, configuration_hash, corpus_hash, index_hash,
                dataset_hash, price_snapshot, repetitions, evidence_token_budget,
                seed, maximum_cost_usd, frozen_at)
               VALUES (%s, 'budget-test', %s, %s, %s, %s, %s, %s::jsonb, 1, 6400,
                       1, 1, now())""",
            (
                experiment_id,
                "a" * 40,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                json.dumps(
                    {
                        "input_per_million_usd": 0.14,
                        "cache_hit_input_per_million_usd": 0.0028,
                        "output_per_million_usd": 0.28,
                        "max_attempts": 3,
                    }
                ),
            ),
        )
        connection.execute(
            """INSERT INTO agent.ingestion_jobs
               (id, document_id, status, force, mode, attempts, retry_budget_consumed,
                is_active, metrics, experiment_id)
               VALUES (%s, %s, 'running', true, 'reextract_all', 1, 0, true, '{}'::jsonb, %s)""",
            (job_id, document_id, experiment_id),
        )
        connection.execute(
            """INSERT INTO agent.ingestion_attempts
               (id, job_id, attempt_number, trace_id, worker_id, status, artifact_action,
                telemetry_complete, retry_budget_consumed)
               VALUES (%s, %s, 1, %s, 'budget-worker', 'running', 'none', false, false)""",
            (attempt_id, job_id, uuid.uuid4().hex),
        )
        parameters = {
            "query_run_id": None,
            "ingestion_attempt_id": attempt_id,
            "reservation_id": reservation_id,
            "logical_call_id": logical_call_id,
            "input_token_limit": DEEPSEEK_MAX_INPUT_TOKENS,
            "output_token_limit": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "attempt_limit": 3,
            "legacy_max_input_tokens": DEEPSEEK_MAX_INPUT_TOKENS,
            "legacy_max_output_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "legacy_query_max_attempts": 3,
        }
        row = connection.execute(_RESERVE_MODEL_CALL_SQL, parameters).fetchone()
        assert row == (
            reservation_id,
            DEEPSEEK_MAX_INPUT_TOKENS,
            DEEPSEEK_MAX_OUTPUT_TOKENS,
            3,
        )
        assert connection.execute(
            """SELECT reserved_cost_usd, status
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (reservation_id,),
        ).fetchone() == (Decimal("0.742560"), "reserved")

        connection.execute("SAVEPOINT over_cap")
        with pytest.raises(psycopg.errors.RaiseException, match="experiment cost cap exceeded"):
            connection.execute(
                _RESERVE_MODEL_CALL_SQL,
                {
                    **parameters,
                    "reservation_id": uuid.uuid4(),
                    "logical_call_id": uuid.uuid4(),
                },
            ).fetchone()
        connection.execute("ROLLBACK TO SAVEPOINT over_cap")

        connection.execute(
            """INSERT INTO agent.model_calls
               (id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
                logical_call_id, operation, outcome, usage_available,
                cache_attribution_complete, input_tokens, cache_hit_input_tokens,
                cache_miss_input_tokens, cache_write_input_tokens,
                unattributed_input_tokens, output_tokens, reasoning_output_tokens,
                total_tokens, experiment_id, cost_reservation_id)
               VALUES (%s, %s, %s, 'pageindex_generation', 'test', 1, %s,
                       'pageindex_completion', 'completed', true, true,
                       10, 0, 10, 0, 0, 5, 0, 15, %s, %s)""",
            (
                uuid.uuid4(),
                attempt_id,
                uuid.uuid4().hex,
                logical_call_id,
                experiment_id,
                reservation_id,
            ),
        )
        assert connection.execute(
            RECONCILE_MODEL_CALL_RESERVATION_SQL,
            {"cost_reservation_id": reservation_id, "terminal": True},
        ).fetchone() == (reservation_id,)
        assert connection.execute(
            """SELECT measured_cost_usd, status
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (reservation_id,),
        ).fetchone() == (Decimal("0.000003"), "finalized")

        uncertain_reservation_id = uuid.uuid4()
        uncertain_logical_call_id = uuid.uuid4()
        uncertain_parameters = {
            **parameters,
            "reservation_id": uncertain_reservation_id,
            "logical_call_id": uncertain_logical_call_id,
            "input_token_limit": 1_000,
            "output_token_limit": 100,
        }
        assert connection.execute(
            _RESERVE_MODEL_CALL_SQL,
            uncertain_parameters,
        ).fetchone() == (uncertain_reservation_id, 1_000, 100, 3)
        connection.execute(
            """INSERT INTO agent.model_calls
               (id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
                logical_call_id, operation, outcome, usage_available,
                cache_attribution_complete, experiment_id, cost_reservation_id)
               VALUES (%s, %s, %s, 'pageindex_generation', 'test', 1, %s,
                       'pageindex_completion', 'failed', false, false, %s, %s)""",
            (
                uuid.uuid4(),
                attempt_id,
                uuid.uuid4().hex,
                uncertain_logical_call_id,
                experiment_id,
                uncertain_reservation_id,
            ),
        )
        assert connection.execute(
            RECONCILE_MODEL_CALL_RESERVATION_SQL,
            {"cost_reservation_id": uncertain_reservation_id, "terminal": True},
        ).fetchone() == (uncertain_reservation_id,)
        assert connection.execute(
            """SELECT reserved_cost_usd, measured_cost_usd, status
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (uncertain_reservation_id,),
        ).fetchone() == (Decimal("0.000168"), None, "review_required")

        call_insert = """
            INSERT INTO agent.model_calls
              (id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
               logical_call_id, operation, outcome, usage_available,
               cache_attribution_complete, experiment_id, cost_reservation_id)
            VALUES (%s, %s, %s, 'pageindex_generation', 'test', %s, %s,
                    'pageindex_completion', 'failed', false, false, %s, %s)
        """
        connection.execute("SAVEPOINT terminal_reservation")
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="model call conflicts with its cost reservation",
        ):
            connection.execute(
                call_insert,
                (
                    uuid.uuid4(),
                    attempt_id,
                    uuid.uuid4().hex,
                    1,
                    uncertain_logical_call_id,
                    experiment_id,
                    uncertain_reservation_id,
                ),
            )
        connection.execute("ROLLBACK TO SAVEPOINT terminal_reservation")

        connection.execute("SAVEPOINT mismatched_logical_call")
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="model call conflicts with its cost reservation",
        ):
            connection.execute(
                call_insert,
                (
                    uuid.uuid4(),
                    attempt_id,
                    uuid.uuid4().hex,
                    2,
                    uuid.uuid4(),
                    experiment_id,
                    uncertain_reservation_id,
                ),
            )
        connection.execute("ROLLBACK TO SAVEPOINT mismatched_logical_call")

        connection.execute("SAVEPOINT attempt_over_limit")
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="model call conflicts with its cost reservation",
        ):
            connection.execute(
                call_insert,
                (
                    uuid.uuid4(),
                    attempt_id,
                    uuid.uuid4().hex,
                    4,
                    uncertain_logical_call_id,
                    experiment_id,
                    uncertain_reservation_id,
                ),
            )
        connection.execute("ROLLBACK TO SAVEPOINT attempt_over_limit")

        connection.execute("SAVEPOINT missing_experiment_owner")
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="model call conflicts with its cost reservation",
        ):
            connection.execute(
                call_insert,
                (
                    uuid.uuid4(),
                    attempt_id,
                    uuid.uuid4().hex,
                    2,
                    uncertain_logical_call_id,
                    None,
                    uncertain_reservation_id,
                ),
            )
        connection.execute("ROLLBACK TO SAVEPOINT missing_experiment_owner")

        connection.execute("SAVEPOINT mutate_reservation_identity")
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="cost reservation identity and bounds are immutable",
        ):
            connection.execute(
                """UPDATE agent.model_call_cost_reservations
                   SET experiment_id = %s, logical_call_id = %s
                   WHERE id = %s""",
                (uuid.uuid4(), uuid.uuid4(), uncertain_reservation_id),
            )
        connection.execute("ROLLBACK TO SAVEPOINT mutate_reservation_identity")

        connection.execute("SAVEPOINT mutate_reservation_bounds")
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="cost reservation identity and bounds are immutable",
        ):
            connection.execute(
                """UPDATE agent.model_call_cost_reservations
                   SET input_token_limit = input_token_limit + 1
                   WHERE id = %s""",
                (uncertain_reservation_id,),
            )
        connection.execute("ROLLBACK TO SAVEPOINT mutate_reservation_bounds")

        connection.execute("SAVEPOINT mutate_ingestion_experiment")
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="ingestion job experiment binding is immutable",
        ):
            connection.execute(
                "UPDATE agent.ingestion_jobs SET experiment_id = NULL WHERE id = %s",
                (job_id,),
            )
        connection.execute("ROLLBACK TO SAVEPOINT mutate_ingestion_experiment")

        connection.rollback()


@pytest.mark.integration
@pytest.mark.skipif(
    not OWNER_URL or not APP_URL,
    reason="PostgreSQL owner and application URLs not set",
)
def test_application_role_cannot_reparent_ledger_ownership() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    app_sync = APP_URL.replace("postgresql+psycopg://", "postgresql://")
    document_id = uuid.uuid4()
    experiment_id = uuid.uuid4()
    bound_job_id = uuid.uuid4()
    unbound_job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    call_id = uuid.uuid4()
    reservation_ids = [uuid.uuid4(), uuid.uuid4()]
    logical_call_ids = [uuid.uuid4(), uuid.uuid4()]
    digest = document_id.hex + document_id.hex

    try:
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                """INSERT INTO agent.documents
                   (id, sha256, original_filename, pdf_object_key, status)
                   VALUES (%s, %s, 'attempt-owner.pdf', %s, 'indexing')""",
                (document_id, digest, f"pdf/{document_id}"),
            )
            connection.execute(
                """INSERT INTO agent.experiments
                   (id, name, release_sha, configuration_hash, corpus_hash, index_hash,
                    dataset_hash, price_snapshot, repetitions, evidence_token_budget,
                    seed, maximum_cost_usd, frozen_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 1, 6400,
                           1, 10, now())""",
                (
                    experiment_id,
                    f"attempt-owner-{experiment_id}",
                    "a" * 40,
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                    json.dumps(
                        {
                            "input_per_million_usd": 1,
                            "cache_hit_input_per_million_usd": 0.1,
                            "output_per_million_usd": 2,
                            "max_input_tokens": 100,
                            "max_output_tokens": 100,
                            "max_attempts": 3,
                        }
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO agent.ingestion_jobs
                   (id, document_id, status, force, mode, attempts, retry_budget_consumed,
                    is_active, metrics, experiment_id)
                   VALUES (%s, %s, 'running', true, 'reextract_all', 1, 0, true,
                           '{}'::jsonb, %s),
                          (%s, %s, 'failed', true, 'reextract_all', 1, 1, false,
                           '{}'::jsonb, NULL)""",
                (
                    bound_job_id,
                    document_id,
                    experiment_id,
                    unbound_job_id,
                    document_id,
                ),
            )
            connection.execute(
                """INSERT INTO agent.ingestion_attempts
                   (id, job_id, attempt_number, trace_id, worker_id, status,
                    artifact_action, telemetry_complete, retry_budget_consumed)
                   VALUES (%s, %s, 1, %s, 'attempt-owner-worker', 'running',
                           'none', false, false)""",
                (attempt_id, bound_job_id, uuid.uuid4().hex),
            )
            for reservation_id, logical_call_id in zip(
                reservation_ids, logical_call_ids, strict=True
            ):
                assert connection.execute(
                    _RESERVE_MODEL_CALL_SQL,
                    {
                        "query_run_id": None,
                        "ingestion_attempt_id": attempt_id,
                        "reservation_id": reservation_id,
                        "logical_call_id": logical_call_id,
                        "input_token_limit": 100,
                        "output_token_limit": 100,
                        "attempt_limit": 3,
                        "legacy_max_input_tokens": DEEPSEEK_MAX_INPUT_TOKENS,
                        "legacy_max_output_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
                        "legacy_query_max_attempts": 3,
                    },
                ).fetchone() == (reservation_id, 100, 100, 3)
            connection.execute(
                """INSERT INTO agent.model_calls
                   (id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
                    logical_call_id, operation, experiment_id, cost_reservation_id,
                    outcome, usage_available, cache_attribution_complete)
                   VALUES (%s, %s, %s, 'pageindex_generation', 'test', 1, %s,
                           'pageindex_completion', %s, %s, 'failed', false, false)""",
                (
                    call_id,
                    attempt_id,
                    uuid.uuid4().hex,
                    logical_call_ids[0],
                    experiment_id,
                    reservation_ids[0],
                ),
            )
            connection.commit()

        with psycopg.connect(app_sync) as connection:
            assert connection.execute(
                """UPDATE agent.ingestion_attempts
                   SET job_id = job_id WHERE id = %s
                   RETURNING job_id""",
                (attempt_id,),
            ).fetchone() == (bound_job_id,)
            connection.execute("SAVEPOINT reparent_ingestion_attempt")
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="ingestion attempt job binding is immutable",
            ):
                connection.execute(
                    "UPDATE agent.ingestion_attempts SET job_id = %s WHERE id = %s",
                    (unbound_job_id, attempt_id),
                )
            connection.execute("ROLLBACK TO SAVEPOINT reparent_ingestion_attempt")

            connection.execute("SAVEPOINT detach_model_call")
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="model call identity is immutable",
            ):
                connection.execute(
                    """UPDATE agent.model_calls
                       SET experiment_id = NULL, cost_reservation_id = NULL
                       WHERE id = %s""",
                    (call_id,),
                )
            connection.execute("ROLLBACK TO SAVEPOINT detach_model_call")

            connection.execute("SAVEPOINT reassign_model_call")
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="model call identity is immutable",
            ):
                connection.execute(
                    """UPDATE agent.model_calls
                       SET logical_call_id = %s, cost_reservation_id = %s
                       WHERE id = %s""",
                    (logical_call_ids[1], reservation_ids[1], call_id),
                )
            connection.execute("ROLLBACK TO SAVEPOINT reassign_model_call")
            connection.commit()

        with psycopg.connect(owner_sync) as connection:
            assert connection.execute(
                "SELECT job_id FROM agent.ingestion_attempts WHERE id = %s",
                (attempt_id,),
            ).fetchone() == (bound_job_id,)
            assert connection.execute(
                """SELECT logical_call_id, experiment_id, cost_reservation_id
                   FROM agent.model_calls WHERE id = %s""",
                (call_id,),
            ).fetchone() == (logical_call_ids[0], experiment_id, reservation_ids[0])
    finally:
        with psycopg.connect(owner_sync, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM agent.model_calls WHERE id = %s",
                (call_id,),
            )
            connection.execute(
                "DELETE FROM agent.model_call_cost_reservations WHERE id = ANY(%s)",
                (reservation_ids,),
            )
            connection.execute(
                "DELETE FROM agent.ingestion_attempts WHERE id = %s",
                (attempt_id,),
            )
            connection.execute(
                "DELETE FROM agent.ingestion_jobs WHERE id = ANY(%s)",
                ([bound_job_id, unbound_job_id],),
            )
            connection.execute(
                "DELETE FROM agent.experiments WHERE id = %s",
                (experiment_id,),
            )
            connection.execute(
                "DELETE FROM agent.documents WHERE id = %s",
                (document_id,),
            )


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
def test_concurrent_reservations_serialize_at_the_experiment_cap() -> None:
    assert OWNER_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    experiment_id = uuid.uuid4()
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    job_ids = [uuid.uuid4(), uuid.uuid4()]
    attempt_ids = [uuid.uuid4(), uuid.uuid4()]
    reservation_ids = [uuid.uuid4(), uuid.uuid4()]
    logical_call_ids = [uuid.uuid4(), uuid.uuid4()]
    with psycopg.connect(owner_sync) as connection:
        connection.execute(
            """INSERT INTO agent.experiments
               (id, name, release_sha, configuration_hash, corpus_hash, index_hash,
                dataset_hash, price_snapshot, repetitions, evidence_token_budget,
                seed, maximum_cost_usd, frozen_at)
               VALUES (%s, 'concurrent-budget-test', %s, %s, %s, %s, %s, %s::jsonb,
                       1, 6400, 1, 1, now())""",
            (
                experiment_id,
                "a" * 40,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                json.dumps(
                    {
                        "input_per_million_usd": 1,
                        "cache_hit_input_per_million_usd": 0.1,
                        "output_per_million_usd": 0,
                        "max_input_tokens": 1_000_000,
                        "max_output_tokens": 1,
                        "max_attempts": 1,
                    }
                ),
            ),
        )
        for index, document_id in enumerate(document_ids):
            digest = document_id.hex + document_id.hex
            connection.execute(
                """INSERT INTO agent.documents
                   (id, sha256, original_filename, pdf_object_key, status)
                   VALUES (%s, %s, %s, %s, 'indexing')""",
                (
                    document_id,
                    digest,
                    f"concurrent-budget-{index}.pdf",
                    f"pdf/concurrent-budget-{document_id}",
                ),
            )
            connection.execute(
                """INSERT INTO agent.ingestion_jobs
                   (id, document_id, status, force, mode, attempts, retry_budget_consumed,
                    is_active, metrics, experiment_id)
                   VALUES (%s, %s, 'running', true, 'reextract_all', 1, 0, true,
                           '{}'::jsonb, %s)""",
                (job_ids[index], document_id, experiment_id),
            )
            connection.execute(
                """INSERT INTO agent.ingestion_attempts
                   (id, job_id, attempt_number, trace_id, worker_id, status, artifact_action,
                    telemetry_complete, retry_budget_consumed)
                   VALUES (%s, %s, 1, %s, 'concurrent-budget-worker', 'running', 'none',
                           false, false)""",
                (attempt_ids[index], job_ids[index], uuid.uuid4().hex),
            )
        connection.commit()

    start = threading.Barrier(2)
    winner_holds_lock = threading.Event()
    release_winner = threading.Event()

    def reserve(index: int) -> str:
        parameters = {
            "query_run_id": None,
            "ingestion_attempt_id": attempt_ids[index],
            "reservation_id": reservation_ids[index],
            "logical_call_id": logical_call_ids[index],
            "input_token_limit": 1_000_000,
            "output_token_limit": 1,
            "attempt_limit": 1,
            "legacy_max_input_tokens": DEEPSEEK_MAX_INPUT_TOKENS,
            "legacy_max_output_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "legacy_query_max_attempts": 3,
        }
        with psycopg.connect(owner_sync) as connection:
            start.wait(timeout=10)
            try:
                row = connection.execute(_RESERVE_MODEL_CALL_SQL, parameters).fetchone()
            except psycopg.errors.RaiseException as error:
                connection.rollback()
                assert "experiment cost cap exceeded" in str(error)
                return "denied"
            assert row == (reservation_ids[index], 1_000_000, 1, 1)
            winner_holds_lock.set()
            assert release_winner.wait(timeout=10)
            connection.commit()
            return "reserved"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, index) for index in range(2)]
            try:
                assert winner_holds_lock.wait(timeout=10)
                assert not any(future.done() for future in futures)
            finally:
                release_winner.set()
            outcomes = [future.result(timeout=10) for future in futures]

        assert sorted(outcomes) == ["denied", "reserved"]
        with psycopg.connect(owner_sync) as connection:
            assert connection.execute(
                """SELECT count(*), coalesce(sum(reserved_cost_usd), 0)
                   FROM agent.model_call_cost_reservations
                   WHERE experiment_id = %s""",
                (experiment_id,),
            ).fetchone() == (1, Decimal("1.000000"))
    finally:
        release_winner.set()
        with psycopg.connect(owner_sync) as connection:
            connection.execute(
                "DELETE FROM agent.model_call_cost_reservations WHERE experiment_id = %s",
                (experiment_id,),
            )
            connection.execute(
                "DELETE FROM agent.ingestion_attempts WHERE id = ANY(%s)",
                (attempt_ids,),
            )
            connection.execute(
                "DELETE FROM agent.ingestion_jobs WHERE id = ANY(%s)",
                (job_ids,),
            )
            connection.execute(
                "DELETE FROM agent.experiments WHERE id = %s",
                (experiment_id,),
            )
            connection.execute(
                "DELETE FROM agent.documents WHERE id = ANY(%s)",
                (document_ids,),
            )
            connection.commit()


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
def test_query_experiment_reservations_are_non_null_capped_and_valid_only() -> None:
    assert OWNER_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    api_key_id = uuid.uuid4()
    experiment_id = uuid.uuid4()
    run_id = uuid.uuid4()
    first_reservation_id = uuid.uuid4()
    with psycopg.connect(owner_sync) as connection:
        connection.execute(
            """INSERT INTO agent.api_keys
               (id, name, prefix, key_hash, scopes, disabled)
               VALUES (%s, 'query-budget-test', %s, %s, '{}'::text[], true)""",
            (api_key_id, f"t{api_key_id.hex[:8]}", api_key_id.hex + api_key_id.hex),
        )
        connection.execute(
            """INSERT INTO agent.experiments
               (id, name, release_sha, configuration_hash, corpus_hash, index_hash,
                dataset_hash, price_snapshot, repetitions, evidence_token_budget,
                seed, maximum_cost_usd, frozen_at)
               VALUES (%s, 'query-budget-test', %s, %s, %s, %s, %s, %s::jsonb, 1, 6400,
                       1, 8, now())""",
            (
                experiment_id,
                "a" * 40,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                json.dumps(
                    {
                        "input_per_million_usd": 1,
                        "cache_hit_input_per_million_usd": 0.1,
                        "output_per_million_usd": 2,
                    }
                ),
            ),
        )
        connection.execute(
            """INSERT INTO agent.query_runs
               (id, trace_id, thread_id, api_key_id, question)
               VALUES (%s, %s, 'query-budget-thread', %s, 'question')""",
            (run_id, uuid.uuid4().hex, api_key_id),
        )
        connection.execute(
            """INSERT INTO agent.experiment_memberships
               (id, experiment_id, query_run_id, trial_key, repetition,
                sequence_number, included)
               VALUES (%s, %s, %s, %s, 1, 0, true)""",
            (uuid.uuid4(), experiment_id, run_id, str(uuid.uuid4())),
        )
        parameters = {
            "query_run_id": run_id,
            "ingestion_attempt_id": None,
            "reservation_id": first_reservation_id,
            "logical_call_id": uuid.uuid4(),
            "input_token_limit": DEEPSEEK_MAX_INPUT_TOKENS,
            "output_token_limit": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "attempt_limit": 3,
            "legacy_max_input_tokens": DEEPSEEK_MAX_INPUT_TOKENS,
            "legacy_max_output_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "legacy_query_max_attempts": 3,
        }
        assert connection.execute(_RESERVE_MODEL_CALL_SQL, parameters).fetchone() == (
            first_reservation_id,
            DEEPSEEK_MAX_INPUT_TOKENS,
            DEEPSEEK_MAX_OUTPUT_TOKENS,
            3,
        )
        assert connection.execute(
            """SELECT reserved_cost_usd
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (first_reservation_id,),
        ).fetchone() == (Decimal("5.304000"),)

        connection.execute("SAVEPOINT over_cap")
        with pytest.raises(psycopg.errors.RaiseException, match="experiment cost cap exceeded"):
            connection.execute(
                _RESERVE_MODEL_CALL_SQL,
                {
                    **parameters,
                    "reservation_id": uuid.uuid4(),
                    "logical_call_id": uuid.uuid4(),
                },
            ).fetchone()
        connection.execute("ROLLBACK TO SAVEPOINT over_cap")

        connection.execute(
            """UPDATE agent.experiments
               SET valid = false, invalid_reason = 'test'
               WHERE id = %s""",
            (experiment_id,),
        )
        assert connection.execute(
            _RESERVE_MODEL_CALL_SQL,
            {
                **parameters,
                "reservation_id": uuid.uuid4(),
                "logical_call_id": uuid.uuid4(),
            },
        ).fetchone() == (
            None,
            None,
            None,
            None,
        )
        connection.rollback()
