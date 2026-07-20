from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vectorless_rag.config import Settings
from vectorless_rag.models import IngestionJob, JobStatus


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


async def claim_ingestion_job(session: AsyncSession, worker_id: str) -> IngestionJob | None:
    """Atomically claim one queued job; concurrent workers skip locked rows."""
    statement = (
        select(IngestionJob)
        .where(IngestionJob.status == JobStatus.queued)
        .order_by(IngestionJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = await session.scalar(statement)
    if job is None:
        return None
    job.status = JobStatus.running
    job.worker_id = worker_id
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    await session.flush()
    return job
