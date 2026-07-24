from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vectorless_rag.config import Settings
from vectorless_rag.models import (
    ArtifactAction,
    Document,
    DocumentStatus,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    JobStatus,
)
from vectorless_rag.operational_logging import emit_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionClaim:
    job_id: uuid.UUID
    document_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int
    trace_id: str
    worker_id: str
    force: bool


def make_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=5)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = make_engine(settings.database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self.sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self.engine.dispose()


async def claim_ingestion_job(
    session: AsyncSession, worker_id: str, settings: Settings
) -> IngestionClaim | None:
    """Atomically claim queued or abandoned work; concurrent workers skip locked rows."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(
        seconds=settings.pageindex_timeout_seconds + settings.ingestion_stale_grace_seconds
    )
    statement = (
        select(IngestionJob)
        .where(
            or_(
                IngestionJob.status == JobStatus.queued,
                and_(
                    IngestionJob.status == JobStatus.running,
                    IngestionJob.started_at.is_not(None),
                    IngestionJob.started_at < stale_before,
                ),
            )
        )
        .order_by(IngestionJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = await session.scalar(statement)
    if job is None:
        return None
    previous_attempt = await session.scalar(
        select(IngestionAttempt)
        .where(
            IngestionAttempt.job_id == job.id,
            IngestionAttempt.status == IngestionAttemptStatus.running,
        )
        .order_by(IngestionAttempt.attempt_number.desc())
        .with_for_update()
        .limit(1)
    )
    if job.attempts >= settings.ingestion_max_attempts:
        document = await session.get(Document, job.document_id, with_for_update=True)
        diagnostic = "Ingestion recovery attempts were exhausted; retry explicitly to continue."
        job.status = JobStatus.failed
        job.worker_id = None
        job.completed_at = now
        job.diagnostic = diagnostic
        job.metrics = {**job.metrics, "failure_code": "ingestion_recovery_exhausted"}
        if previous_attempt is not None:
            elapsed_seconds = max(0.0, (now - previous_attempt.started_at).total_seconds())
            previous_attempt.status = IngestionAttemptStatus.failed
            previous_attempt.failure_code = "ingestion_recovery_exhausted"
            previous_attempt.completed_at = now
            previous_attempt.elapsed_seconds = elapsed_seconds
            emit_event(
                logger,
                "ingestion_attempt_failed",
                job_id=job.id,
                document_id=job.document_id,
                attempt_id=previous_attempt.id,
                trace_id=previous_attempt.trace_id,
                worker_id=previous_attempt.worker_id,
                status=IngestionAttemptStatus.failed.value,
                failure_code="ingestion_recovery_exhausted",
                elapsed_ms=round(elapsed_seconds * 1000),
                telemetry_complete=False,
                release=settings.release,
            )
        if document is not None:
            document.status = DocumentStatus.failed
            document.diagnostic = diagnostic
        await session.flush()
        return None
    if previous_attempt is not None:
        elapsed_seconds = max(0.0, (now - previous_attempt.started_at).total_seconds())
        previous_attempt.status = IngestionAttemptStatus.abandoned
        previous_attempt.failure_code = "stale_attempt_reclaimed"
        previous_attempt.completed_at = now
        previous_attempt.elapsed_seconds = elapsed_seconds
        emit_event(
            logger,
            "ingestion_attempt_abandoned",
            job_id=job.id,
            document_id=job.document_id,
            attempt_id=previous_attempt.id,
            trace_id=previous_attempt.trace_id,
            worker_id=previous_attempt.worker_id,
            status=IngestionAttemptStatus.abandoned.value,
            failure_code="stale_attempt_reclaimed",
            elapsed_ms=round(elapsed_seconds * 1000),
            telemetry_complete=False,
            release=settings.release,
        )
    job.status = JobStatus.running
    job.worker_id = worker_id
    job.attempts += 1
    job.started_at = now
    job.completed_at = None
    attempt_id = uuid.uuid4()
    trace_id = uuid.uuid4().hex
    attempt = IngestionAttempt(
        id=attempt_id,
        job_id=job.id,
        attempt_number=job.attempts,
        trace_id=trace_id,
        worker_id=worker_id,
        status=IngestionAttemptStatus.running,
        artifact_action=ArtifactAction.none,
        started_at=now,
    )
    session.add(attempt)
    await session.flush()
    return IngestionClaim(
        job_id=job.id,
        document_id=job.document_id,
        attempt_id=attempt_id,
        attempt_number=job.attempts,
        trace_id=trace_id,
        worker_id=worker_id,
        force=job.force,
    )
