from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TypeVar

import psycopg
import pytest
from psycopg import errors
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from vectorless_rag.config import Settings
from vectorless_rag.db import claim_ingestion_job, make_engine
from vectorless_rag.models import Document, DocumentStatus, IngestionJob, JobStatus
from vectorless_rag.retrieval import CorpusRouter
from vectorless_rag.schemas import MetadataConstraints
from vectorless_rag.sql_guard import PostgreSQLAdapter, validate_sql

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
    ) -> StructuredT:
        del schema, system, user, thinking
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
            router = CorpusRouter(Settings(candidate_limit=3), _UnusedRouterLLM())

            precision_ids = [document.id for document in documents[:3]]
            precision = await router.candidates(
                session,
                f"{precision_alpha} {precision_beta}",
                MetadataConstraints(),
                precision_ids,
            )
            assert [candidate.document.id for candidate in precision] == [
                documents[0].id,
                documents[2].id,
                documents[1].id,
            ]
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
            started_at=now - timedelta(seconds=120),
            created_at=created_baseline - timedelta(seconds=5),
        ),
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[3],
            status=JobStatus.running,
            attempts=3,
            started_at=now - timedelta(seconds=120),
            created_at=created_baseline - timedelta(seconds=4),
        ),
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[4],
            status=JobStatus.running,
            attempts=1,
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
            await session.commit()

        async with sessions() as first, sessions() as second:
            first_claim = await claim_ingestion_job(first, "worker-1", settings)
            second_claim = await claim_ingestion_job(second, "worker-2", settings)
            assert first_claim is not None and first_claim.id == jobs[0].id
            assert second_claim is not None and second_claim.id == jobs[1].id
            assert first_claim.attempts == 1
            assert second_claim.attempts == 1
            await first.commit()
            await second.commit()

        async with sessions() as session:
            stale_claim = await claim_ingestion_job(session, "worker-3", settings)
            assert stale_claim is not None and stale_claim.id == jobs[2].id
            assert stale_claim.attempts == 2
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

        locked_job = IngestionJob(
            id=uuid.uuid4(),
            document_id=document_ids[5],
            status=JobStatus.running,
            attempts=1,
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
            assert skipped_claim is not None and skipped_claim.id == fallback_job.id
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
                delete(IngestionJob).where(IngestionJob.document_id.in_(document_ids))
            )
            await session.execute(delete(Document).where(Document.id.in_(document_ids)))
            await session.commit()
        await engine.dispose()
