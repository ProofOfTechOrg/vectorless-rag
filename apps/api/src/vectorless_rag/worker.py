from __future__ import annotations

import asyncio
import logging
import os
import socket

from vectorless_rag.config import get_settings
from vectorless_rag.db import Database, claim_ingestion_job
from vectorless_rag.ingestion import IngestionProcessor
from vectorless_rag.llm import DeepSeekLLM
from vectorless_rag.models import IngestionJob
from vectorless_rag.observability import Observability
from vectorless_rag.pageindex_adapter import PageIndexAdapter
from vectorless_rag.storage import S3ObjectStore

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    database = Database(settings)
    store = S3ObjectStore(settings)
    await store.ensure_bucket()
    observability = Observability(settings)
    processor = IngestionProcessor(
        settings, store, DeepSeekLLM(settings), PageIndexAdapter(settings)
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    try:
        while True:
            job_id = None
            async with database.session() as session:
                job = await claim_ingestion_job(session, worker_id)
                if job is not None:
                    job_id = job.id
            if job_id is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            async with database.session() as session:
                job = await session.get(IngestionJob, job_id, with_for_update=True)
                if job is None:
                    continue
                try:
                    await processor.process(session, job)
                except Exception:
                    logger.exception("ingestion job %s failed", job_id)
    finally:
        observability.flush()
        await database.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
