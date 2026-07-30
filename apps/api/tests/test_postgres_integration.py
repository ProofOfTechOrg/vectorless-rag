from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypeVar, cast

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors
from pydantic import BaseModel
from sqlalchemy import Numeric, delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from vectorless_rag.config import Settings
from vectorless_rag.db import (
    claim_ingestion_job,
    make_engine,
    set_ingestion_provider_admission,
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
    ModelCallStage,
)
from vectorless_rag.provider import DEEPSEEK_MAX_INPUT_TOKENS, DEEPSEEK_MAX_OUTPUT_TOKENS
from vectorless_rag.retrieval import CorpusRouter
from vectorless_rag.schemas import MetadataConstraints
from vectorless_rag.sql_guard import PostgreSQLAdapter, validate_sql
from vectorless_rag.telemetry_classification import classification_report
from vectorless_rag.usage import (
    RECONCILE_MODEL_CALL_RESERVATION_SQL,
    RESERVE_MODEL_CALL_SQL,
    ExperimentBudgetExceeded,
    ModelCallLedger,
    UsageOwner,
    usage_context,
)

OWNER_URL = os.getenv("TEST_OWNER_DATABASE_URL")
APP_URL = os.getenv("TEST_APP_DATABASE_URL")
READER_URL = os.getenv("TEST_DATABASE_URL")
StructuredT = TypeVar("StructuredT", bound=BaseModel)
API_ROOT = Path(__file__).resolve().parents[1]


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
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
def test_0006_migration_backfills_historical_reservation_bounds() -> None:
    assert OWNER_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", OWNER_URL)
    command.downgrade(config, "0005_measurement_experiments")

    explicit_experiment = uuid.uuid4()
    legacy_experiment = uuid.uuid4()
    explicit_reservation = uuid.uuid4()
    legacy_reservation = uuid.uuid4()
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
                uuid.uuid4(),
                legacy_reservation,
                legacy_experiment,
                uuid.uuid4(),
            ),
        )
        assert connection.execute(
            """SELECT price_snapshot->>'max_input_tokens',
                      price_snapshot->>'max_output_tokens',
                      price_snapshot->>'max_attempts'
               FROM agent.experiments WHERE id = %s""",
            (explicit_experiment,),
        ).fetchone() == ("1000000.0", "8192.0", "3.0")
        connection.commit()

    command.upgrade(config, "head")

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
            "0006_bound_cost_reservations",
        )
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
        connection.execute(
            "DELETE FROM agent.model_call_cost_reservations WHERE id = ANY(%s)",
            ([explicit_reservation, legacy_reservation],),
        )
        connection.execute(
            "DELETE FROM agent.experiments WHERE id = ANY(%s)",
            ([explicit_experiment, legacy_experiment],),
        )
        connection.commit()


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
def test_ingestion_experiment_rejects_unreservable_call_before_insert() -> None:
    assert OWNER_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    experiment_id = uuid.uuid4()
    with psycopg.connect(owner_sync) as connection:
        connection.execute(
            """INSERT INTO agent.documents
               (id, sha256, original_filename, pdf_object_key, status)
               VALUES (%s, %s, 'reservation.pdf', 'pdf/reservation', 'indexing')""",
            (document_id, uuid.uuid4().hex + uuid.uuid4().hex),
        )
        connection.execute(
            """INSERT INTO agent.experiments
               (id, name, release_sha, configuration_hash, corpus_hash, index_hash,
                dataset_hash, price_snapshot, repetitions, evidence_token_budget,
                seed, maximum_cost_usd, frozen_at)
               VALUES (%s, 'ingestion-reservation-test', %s, %s, %s, %s, %s,
                       %s::jsonb, 1, 6400, 1, 10, now())""",
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
                        "max_input_tokens": 100,
                        "max_output_tokens": 100,
                        "max_attempts": 1,
                    }
                ),
            ),
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
        connection.commit()

    ledger = ModelCallLedger(OWNER_URL, maximum_provider_attempts=1)
    with usage_context(UsageOwner(trace_id=uuid.uuid4().hex, ingestion_attempt_id=attempt_id)):
        with pytest.raises(ExperimentBudgetExceeded):
            ledger.start(
                ModelCallStage.pageindex_generation,
                "provider/model",
                1,
                input_token_limit=101,
                output_token_limit=1,
            )

    with psycopg.connect(owner_sync) as connection:
        assert connection.execute(
            """SELECT count(*) FROM agent.model_calls
               WHERE ingestion_attempt_id = %s""",
            (attempt_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """SELECT count(*) FROM agent.model_call_cost_reservations
               WHERE experiment_id = %s""",
            (experiment_id,),
        ).fetchone() == (0,)
        connection.execute("DELETE FROM agent.ingestion_attempts WHERE id = %s", (attempt_id,))
        connection.execute("DELETE FROM agent.ingestion_jobs WHERE id = %s", (job_id,))
        connection.execute("DELETE FROM agent.experiments WHERE id = %s", (experiment_id,))
        connection.execute("DELETE FROM agent.documents WHERE id = %s", (document_id,))
        connection.commit()


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
                           'Reasoning based search', ARRAY['rag', 'pageindex'], 'ready')""",
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
        row = connection.execute(RESERVE_MODEL_CALL_SQL, parameters).fetchone()
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
                RESERVE_MODEL_CALL_SQL,
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
                total_tokens, cost_reservation_id)
               VALUES (%s, %s, %s, 'pageindex_generation', 'test', 1, %s,
                       'pageindex_completion', 'completed', true, true,
                       10, 0, 10, 0, 0, 5, 0, 15, %s)""",
            (uuid.uuid4(), attempt_id, uuid.uuid4().hex, logical_call_id, reservation_id),
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
            RESERVE_MODEL_CALL_SQL,
            uncertain_parameters,
        ).fetchone() == (uncertain_reservation_id, 1_000, 100, 3)
        connection.execute(
            """INSERT INTO agent.model_calls
               (id, ingestion_attempt_id, trace_id, stage, model, provider_attempt,
                logical_call_id, operation, outcome, usage_available,
                cache_attribution_complete, cost_reservation_id)
               VALUES (%s, %s, %s, 'pageindex_generation', 'test', 1, %s,
                       'pageindex_completion', 'failed', false, false, %s)""",
            (
                uuid.uuid4(),
                attempt_id,
                uuid.uuid4().hex,
                uncertain_logical_call_id,
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
               cache_attribution_complete, cost_reservation_id)
            VALUES (%s, %s, %s, 'pageindex_generation', 'test', %s, %s,
                    'pageindex_completion', 'failed', false, false, %s)
        """
        connection.execute("SAVEPOINT duplicate_attempt")
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                call_insert,
                (
                    uuid.uuid4(),
                    attempt_id,
                    uuid.uuid4().hex,
                    1,
                    uncertain_logical_call_id,
                    uncertain_reservation_id,
                ),
            )
        connection.execute("ROLLBACK TO SAVEPOINT duplicate_attempt")

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
                    uncertain_reservation_id,
                ),
            )
        connection.execute("ROLLBACK TO SAVEPOINT attempt_over_limit")

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
def test_application_role_cannot_reparent_an_ingestion_attempt() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    owner_sync = OWNER_URL.replace("postgresql+psycopg://", "postgresql://")
    app_sync = APP_URL.replace("postgresql+psycopg://", "postgresql://")
    document_id = uuid.uuid4()
    experiment_id = uuid.uuid4()
    bound_job_id = uuid.uuid4()
    unbound_job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
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
            connection.commit()

        with psycopg.connect(owner_sync) as connection:
            assert connection.execute(
                "SELECT job_id FROM agent.ingestion_attempts WHERE id = %s",
                (attempt_id,),
            ).fetchone() == (bound_job_id,)
    finally:
        with psycopg.connect(owner_sync, autocommit=True) as connection:
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
                row = connection.execute(RESERVE_MODEL_CALL_SQL, parameters).fetchone()
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
        assert connection.execute(RESERVE_MODEL_CALL_SQL, parameters).fetchone() == (
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
                RESERVE_MODEL_CALL_SQL,
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
            RESERVE_MODEL_CALL_SQL,
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
