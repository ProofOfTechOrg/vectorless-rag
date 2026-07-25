from __future__ import annotations

import asyncio
import logging
import os
import socket

from vectorless_rag.config import get_settings
from vectorless_rag.db import (
    Database,
    IngestionClaim,
    abandon_ingestion_claim,
    claim_ingestion_job,
    renew_ingestion_lease,
    set_ingestion_provider_admission,
)
from vectorless_rag.ingestion import (
    CompletedBuild,
    IngestionLeaseLost,
    IngestionProcessor,
    PreparedIngestion,
)
from vectorless_rag.llm import DeepSeekLLM
from vectorless_rag.models import (
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
)
from vectorless_rag.observability import Observability
from vectorless_rag.operational_logging import emit_event
from vectorless_rag.pageindex_adapter import PageIndexAdapter
from vectorless_rag.storage import S3ObjectStore

logger = logging.getLogger(__name__)


async def _build_with_heartbeat(
    database: Database,
    processor: IngestionProcessor,
    claim: IngestionClaim,
    prepared: PreparedIngestion,
    heartbeat_seconds: int,
) -> CompletedBuild | None:
    task = asyncio.create_task(processor.build_claim(prepared))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=heartbeat_seconds)
            if task in done:
                return await task
            async with database.session() as session:
                renewed = await renew_ingestion_lease(
                    session,
                    claim,
                    processor.settings,
                    stage="building",
                )
            if not renewed:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise IngestionLeaseLost("ingestion lease expired during build")
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise


async def _event_fields(
    database: Database,
    claim: IngestionClaim,
    release: str,
) -> tuple[str, dict[str, object]]:
    async with database.session() as session:
        job = await session.get(IngestionJob, claim.job_id)
        attempt = await session.get(IngestionAttempt, claim.attempt_id)
        if job is None or attempt is None:
            return "ingestion_attempt_missing", {
                "job_id": claim.job_id,
                "attempt_id": claim.attempt_id,
                "release": release,
            }
        terminal_status = IngestionAttemptStatus(attempt.status.value)
        event_name = (
            "ingestion_attempt_completed"
            if terminal_status == IngestionAttemptStatus.completed
            else (
                "ingestion_attempt_deferred"
                if terminal_status
                in (IngestionAttemptStatus.deferred, IngestionAttemptStatus.abandoned)
                else "ingestion_attempt_failed"
            )
        )
        fields: dict[str, object] = {
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
            "release": release,
        }
        if attempt.page_count is not None:
            fields["page_count"] = attempt.page_count
        if attempt.failure_code is not None:
            fields["failure_code"] = attempt.failure_code
        if attempt.index_version_id is not None:
            fields["index_version_id"] = attempt.index_version_id
        return event_name, fields


async def run_worker() -> None:
    settings = get_settings()
    database = Database(settings)
    store = S3ObjectStore(settings)
    await store.ensure_bucket()
    observability = Observability(settings)
    llm = DeepSeekLLM(settings, observability)
    processor = IngestionProcessor(settings, store, llm, PageIndexAdapter(settings))
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    try:
        while True:
            async with database.session() as session:
                provider = await llm.provider_gate.status(session, refresh=False)
                await set_ingestion_provider_admission(
                    session,
                    available=provider.admits_model_work,
                    reason=provider.reason or provider.availability.value,
                    checked_at=provider.checked_at,
                )
                if not provider.admits_model_work:
                    claim = None
                else:
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
            prepared: PreparedIngestion | None = None
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
                    async with database.session() as session:
                        prepared = await processor.prepare_claim(
                            session,
                            job_id=claim.job_id,
                            attempt_id=claim.attempt_id,
                            worker_id=claim.worker_id,
                            lease_token=claim.lease_token,
                        )
                    if prepared.reusable_index_version_id is not None:
                        async with database.session() as session:
                            await processor.verify_reusable(session, prepared)
                        build = None
                    else:
                        build = await _build_with_heartbeat(
                            database,
                            processor,
                            claim,
                            prepared,
                            settings.ingestion_heartbeat_seconds,
                        )
                    async with database.session() as session:
                        await processor.activate_claim(session, prepared, build)
                except asyncio.CancelledError as error:
                    if prepared is not None:
                        async with database.session() as session:
                            await processor.fail_claim(session, prepared, error)
                    else:
                        async with database.session() as session:
                            await abandon_ingestion_claim(
                                session,
                                claim,
                                failure_code="worker_cancelled",
                            )
                    raise
                except IngestionLeaseLost:
                    async with database.session() as session:
                        await abandon_ingestion_claim(
                            session,
                            claim,
                            failure_code="lease_lost",
                        )
                except BaseException as error:
                    if prepared is not None:
                        async with database.session() as session:
                            await processor.fail_claim(session, prepared, error)
                    else:
                        async with database.session() as session:
                            await abandon_ingestion_claim(
                                session,
                                claim,
                                failure_code="prepare_failed",
                            )
            event_name, event_fields = await _event_fields(database, claim, settings.release)
            emit_event(logger, event_name, **event_fields)
    finally:
        observability.flush()
        await database.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
