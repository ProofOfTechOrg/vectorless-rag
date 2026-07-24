from __future__ import annotations

import asyncio
import logging
import os
import socket

from vectorless_rag.config import get_settings
from vectorless_rag.db import Database, claim_ingestion_job
from vectorless_rag.ingestion import IngestionProcessor
from vectorless_rag.llm import DeepSeekLLM
from vectorless_rag.models import (
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    JobStatus,
)
from vectorless_rag.observability import Observability
from vectorless_rag.operational_logging import emit_event
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
        settings, store, DeepSeekLLM(settings, observability), PageIndexAdapter(settings)
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    try:
        while True:
            claim = None
            async with database.session() as session:
                claim = await claim_ingestion_job(session, worker_id, settings)
            if claim is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            emit_event(
                logger,
                "ingestion_attempt_started",
                job_id=claim.job_id,
                document_id=claim.document_id,
                attempt_id=claim.attempt_id,
                trace_id=claim.trace_id,
                worker_id=claim.worker_id,
                status=IngestionAttemptStatus.running.value,
                release=settings.release,
            )
            event_name = "ingestion_attempt_failed"
            event_fields: dict[str, object] = {}
            async with database.session() as session:
                job = await session.get(IngestionJob, claim.job_id, with_for_update=True)
                attempt = await session.get(IngestionAttempt, claim.attempt_id)
                if job is None or attempt is None:
                    continue
                if (
                    job.status != JobStatus.running
                    or job.worker_id != claim.worker_id
                    or attempt.status != IngestionAttemptStatus.running
                ):
                    continue
                with observability.span(
                    "ingestion-attempt",
                    claim.trace_id,
                    metadata={
                        "job_id": str(claim.job_id),
                        "attempt_id": str(claim.attempt_id),
                        "release": settings.release,
                    },
                ):
                    try:
                        await processor.process(session, job, attempt)
                    except Exception:
                        event_name = "ingestion_attempt_failed"
                terminal_status = IngestionAttemptStatus(attempt.status.value)
                event_name = (
                    "ingestion_attempt_completed"
                    if terminal_status == IngestionAttemptStatus.completed
                    else "ingestion_attempt_failed"
                )
                event_fields = {
                    "job_id": job.id,
                    "document_id": job.document_id,
                    "attempt_id": attempt.id,
                    "trace_id": attempt.trace_id,
                    "worker_id": attempt.worker_id,
                    "status": terminal_status.value,
                    "elapsed_ms": round((attempt.elapsed_seconds or 0) * 1000),
                    "model_call_count": int(job.metrics.get("model_call_count", 0)),
                    "input_tokens": int(job.metrics.get("input_tokens", 0)),
                    "output_tokens": int(job.metrics.get("output_tokens", 0)),
                    "telemetry_complete": attempt.telemetry_complete,
                    "artifact_action": attempt.artifact_action.value,
                    "release": settings.release,
                }
                if attempt.page_count is not None:
                    event_fields["page_count"] = attempt.page_count
                if attempt.failure_code is not None:
                    event_fields["failure_code"] = attempt.failure_code
            emit_event(logger, event_name, **event_fields)
    finally:
        observability.flush()
        await database.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
