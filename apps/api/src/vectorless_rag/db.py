from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
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
    ExperimentMembership,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    IngestionMode,
    JobStatus,
)
from vectorless_rag.operational_logging import emit_event
from vectorless_rag.provider import STATUS_CACHE_WINDOW

logger = logging.getLogger(__name__)


def driver_free_url(url: str) -> str:
    """Strip SQLAlchemy's driver suffix for libraries that take a raw libpq conninfo.

    psycopg's pool and LangGraph's checkpointer both parse the URL themselves and reject
    SQLAlchemy's ``+psycopg`` dialect marker.
    """
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass(frozen=True)
class IngestionClaim:
    job_id: uuid.UUID
    document_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int
    trace_id: str
    worker_id: str
    mode: IngestionMode
    lease_token: uuid.UUID
    lease_expires_at: datetime

    @property
    def force(self) -> bool:
        return self.mode != IngestionMode.ensure_current


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


async def set_ingestion_provider_admission(
    session: AsyncSession,
    *,
    available: bool,
    reason: str | None,
    checked_at: datetime,
) -> None:
    if available:
        transitioned = await session.execute(
            update(IngestionJob)
            .where(
                IngestionJob.status == JobStatus.waiting_provider,
                IngestionJob.is_active.is_(True),
                IngestionJob.updated_at <= checked_at - STATUS_CACHE_WINDOW,
            )
            .values(
                status=JobStatus.queued,
                provider_wait_reason=None,
                current_stage="queued",
            )
            .returning(IngestionJob.document_id)
        )
        document_ids = set(transitioned.scalars().all())
        if not document_ids:
            return
        transitioned_documents = list(
            (
                await session.scalars(
                    select(Document).where(Document.id.in_(document_ids)).with_for_update()
                )
            ).all()
        )
        for document in transitioned_documents:
            if document.active_index_version_id is None:
                document.status = DocumentStatus.queued
            document.diagnostic = None
        return

    jobs = list(
        (
            await session.scalars(
                select(IngestionJob)
                .where(
                    IngestionJob.status == JobStatus.queued,
                    IngestionJob.is_active.is_(True),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    safe_reason = (reason or "provider_state_unknown")[:80]
    for job in jobs:
        job.status = JobStatus.waiting_provider
        job.provider_wait_reason = safe_reason
        job.current_stage = "waiting_provider"
        document = await session.get(Document, job.document_id, with_for_update=True)
        if document is not None and document.active_index_version_id is None:
            document.status = DocumentStatus.waiting_provider
            document.diagnostic = "Model work is waiting for provider availability."


async def claim_ingestion_job(
    session: AsyncSession, worker_id: str, settings: Settings
) -> IngestionClaim | None:
    """Atomically claim queued or abandoned work; concurrent workers skip locked rows."""
    now = datetime.now(UTC)
    legacy_stale_before = now - timedelta(
        seconds=settings.pageindex_timeout_seconds + settings.ingestion_stale_grace_seconds
    )
    statement = (
        select(IngestionJob)
        .where(
            or_(
                IngestionJob.status == JobStatus.queued,
                and_(
                    IngestionJob.status == JobStatus.running,
                    or_(
                        IngestionJob.lease_expires_at < now,
                        and_(
                            IngestionJob.lease_expires_at.is_(None),
                            IngestionJob.started_at.is_not(None),
                            IngestionJob.started_at < legacy_stale_before,
                        ),
                    ),
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
    if job.retry_budget_consumed >= settings.ingestion_max_attempts:
        document = await session.get(Document, job.document_id, with_for_update=True)
        diagnostic = "Ingestion recovery attempts were exhausted; retry explicitly to continue."
        job.status = JobStatus.failed
        job.worker_id = None
        job.completed_at = now
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.current_stage = "failed"
        job.active_attempt_id = None
        job.is_active = False
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
            if document.active_index_version_id is None:
                document.status = DocumentStatus.failed
                document.diagnostic = diagnostic
            else:
                document.status = DocumentStatus.ready
                document.diagnostic = None
        await session.flush()
        return None
    if previous_attempt is not None:
        elapsed_seconds = max(0.0, (now - previous_attempt.started_at).total_seconds())
        previous_attempt.status = IngestionAttemptStatus.abandoned
        previous_attempt.failure_code = "stale_attempt_reclaimed"
        previous_attempt.completed_at = now
        previous_attempt.elapsed_seconds = elapsed_seconds
        previous_attempt.retry_budget_consumed = False
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
        await session.flush([previous_attempt])
    job.status = JobStatus.running
    job.worker_id = worker_id
    job.attempts += 1
    job.started_at = now
    job.completed_at = None
    lease_token = uuid.uuid4()
    lease_expires_at = now + timedelta(seconds=settings.ingestion_lease_seconds)
    job.lease_token = lease_token
    job.lease_expires_at = lease_expires_at
    job.heartbeat_at = now
    job.current_stage = "claimed"
    job.provider_wait_reason = None
    job.is_active = True
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
        lease_token=lease_token,
        retry_budget_consumed=False,
    )
    session.add(attempt)
    await session.flush([attempt])
    if job.experiment_id is not None:
        session.add(
            ExperimentMembership(
                experiment_id=job.experiment_id,
                attempt_id=attempt.id,
                trial_key=f"ingestion:{job.document_id}",
                repetition=job.attempts,
                sequence_number=job.attempts - 1,
                included=True,
            )
        )
    job.active_attempt_id = attempt_id
    await session.flush()
    return IngestionClaim(
        job_id=job.id,
        document_id=job.document_id,
        attempt_id=attempt_id,
        attempt_number=job.attempts,
        trace_id=trace_id,
        worker_id=worker_id,
        mode=job.mode,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
    )


async def renew_ingestion_lease(
    session: AsyncSession,
    claim: IngestionClaim,
    settings: Settings,
    *,
    stage: str,
) -> bool:
    now = datetime.now(UTC)
    job = await session.get(IngestionJob, claim.job_id, with_for_update=True)
    if (
        job is None
        or job.status != JobStatus.running
        or job.worker_id != claim.worker_id
        or job.lease_token != claim.lease_token
        or job.active_attempt_id != claim.attempt_id
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        return False
    attempt = await session.get(IngestionAttempt, claim.attempt_id, with_for_update=True)
    if (
        attempt is None
        or attempt.status != IngestionAttemptStatus.running
        or attempt.lease_token != claim.lease_token
    ):
        return False
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=settings.ingestion_lease_seconds)
    job.current_stage = stage[:80]
    await session.flush()
    return True


async def abandon_ingestion_claim(
    session: AsyncSession,
    claim: IngestionClaim,
    *,
    failure_code: str,
) -> None:
    now = datetime.now(UTC)
    job = await session.get(IngestionJob, claim.job_id, with_for_update=True)
    attempt = await session.get(IngestionAttempt, claim.attempt_id, with_for_update=True)
    if (
        job is None
        or attempt is None
        or job.lease_token != claim.lease_token
        or job.active_attempt_id != claim.attempt_id
    ):
        return
    attempt.status = IngestionAttemptStatus.abandoned
    attempt.failure_code = failure_code[:80]
    attempt.completed_at = now
    attempt.elapsed_seconds = max(0.0, (now - attempt.started_at).total_seconds())
    attempt.retry_budget_consumed = False
    job.status = JobStatus.queued
    job.worker_id = None
    job.lease_token = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.current_stage = "queued"
    job.active_attempt_id = None
    job.is_active = True
    await session.flush()
