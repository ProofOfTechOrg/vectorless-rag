from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TypeVar

import psycopg
import pytest
from psycopg import errors
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from vectorless_rag.config import Settings
from vectorless_rag.db import claim_ingestion_job, make_engine
from vectorless_rag.models import (
    Document,
    DocumentStatus,
    IndexLifecycleStatus,
    IndexVersion,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    JobStatus,
    ModelCallStage,
)
from vectorless_rag.retrieval import CorpusRouter
from vectorless_rag.schemas import MetadataConstraints
from vectorless_rag.sql_guard import PostgreSQLAdapter, validate_sql
from vectorless_rag.usage import (
    RECONCILE_MODEL_CALL_RESERVATION_SQL,
    RESERVE_MODEL_CALL_SQL,
)

OWNER_URL = os.getenv("TEST_OWNER_DATABASE_URL")
READER_URL = os.getenv("TEST_DATABASE_URL")
StructuredT = TypeVar("StructuredT", bound=BaseModel)


class _UnusedRouterLLM:
    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
    ) -> StructuredT:
        del schema, system, user, thinking, stage
        raise AssertionError("candidate lookup does not invoke the LLM")


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
                       1, 0.001, now())""",
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
            "prompt_bytes": 10,
        }
        row = connection.execute(RESERVE_MODEL_CALL_SQL, parameters).fetchone()
        assert row == (reservation_id,)
        assert connection.execute(
            """SELECT reserved_cost_usd, status
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (reservation_id,),
        ).fetchone() == (Decimal("0.000630"), "reserved")

        connection.execute("SAVEPOINT over_cap")
        with pytest.raises(psycopg.errors.RaiseException, match="experiment cost cap exceeded"):
            connection.execute(
                RESERVE_MODEL_CALL_SQL,
                {
                    **parameters,
                    "reservation_id": uuid.uuid4(),
                    "logical_call_id": uuid.uuid4(),
                    "prompt_bytes": 1_000,
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
        connection.execute(
            RECONCILE_MODEL_CALL_RESERVATION_SQL,
            {"cost_reservation_id": reservation_id, "terminal": True},
        )
        assert connection.execute(
            """SELECT measured_cost_usd, status
               FROM agent.model_call_cost_reservations WHERE id = %s""",
            (reservation_id,),
        ).fetchone() == (Decimal("0.000020"), "finalized")
        connection.rollback()
