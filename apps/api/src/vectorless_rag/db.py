from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vectorless_rag.config import Settings
from vectorless_rag.models import Document, DocumentStatus, IngestionJob, JobStatus


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
) -> IngestionJob | None:
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
    if job.attempts >= settings.ingestion_max_attempts:
        document = await session.get(Document, job.document_id, with_for_update=True)
        diagnostic = "Ingestion recovery attempts were exhausted; retry explicitly to continue."
        job.status = JobStatus.failed
        job.worker_id = None
        job.completed_at = now
        job.diagnostic = diagnostic
        job.metrics = {**job.metrics, "failure_code": "ingestion_recovery_exhausted"}
        if document is not None:
            document.status = DocumentStatus.failed
            document.diagnostic = diagnostic
        await session.flush()
        return None
    job.status = JobStatus.running
    job.worker_id = worker_id
    job.attempts += 1
    job.started_at = now
    job.completed_at = None
    await session.flush()
    return job
