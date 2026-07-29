#!/usr/bin/env python3
"""Run the frozen PageIndex-v2 pilot one document at a time."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from PyPDF2 import PdfReader
from sqlalchemy import func, select, text

from vectorless_rag.config import get_settings
from vectorless_rag.db import Database
from vectorless_rag.ingestion import configuration_digest, register_pdf
from vectorless_rag.models import (
    Experiment,
    ExperimentIndexVersion,
    IngestionJob,
    IngestionMode,
    JobStatus,
    ModelCallCostReservation,
)
from vectorless_rag.pilot_suite import PilotDocument, load_pilot_manifest
from vectorless_rag.provider import (
    DEEPSEEK_MAX_INPUT_TOKENS,
    ProviderGate,
    provider_configuration_hash,
)
from vectorless_rag.storage import S3ObjectStore

TERMINAL = {
    JobStatus.completed,
    JobStatus.failed,
    JobStatus.unsupported,
    JobStatus.deferred,
    JobStatus.cancelled,
}
RELEASE_SHA = re.compile(r"^[0-9a-f]{40}$")


def report_cost(value: Decimal | float | int) -> float:
    return float(round(value, 6))


def validate_pdf(path: Path, expected: PilotDocument) -> None:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected.pdf_sha256:
        raise ValueError(f"PDF digest differs from the pilot manifest: {expected.arxiv_id}")
    if len(PdfReader(path).pages) != expected.page_count:
        raise ValueError(f"PDF page count differs from the pilot manifest: {expected.arxiv_id}")


async def wait_for_job(database: Database, job_id: uuid.UUID) -> IngestionJob:
    while True:
        async with database.session() as session:
            job = await session.get(IngestionJob, job_id)
            if job is None:
                raise RuntimeError("pilot ingestion job disappeared")
            if job.status in TERMINAL:
                return job
        await asyncio.sleep(2)


async def run(args: argparse.Namespace) -> dict[str, object]:
    settings = get_settings()
    manifest = load_pilot_manifest(args.manifest)
    release_sha = str(args.release_sha)
    if settings.allow_full_corpus_index:
        raise RuntimeError("the pilot requires ALLOW_FULL_CORPUS_INDEX=false")
    if settings.pilot_document_limit != 25 or settings.pilot_cost_limit_usd != 10:
        raise RuntimeError("the pilot requires the frozen 25-document/$10 safety settings")
    if RELEASE_SHA.fullmatch(release_sha) is None:
        raise RuntimeError("the pilot requires an explicit 40-character release SHA")
    if settings.release != release_sha:
        raise RuntimeError("RELEASE must equal the immutable git commit SHA")
    for document in manifest.documents:
        validate_pdf(settings.arxiv_pdf_dir / f"{document.arxiv_id}.pdf", document)

    database = Database(settings)
    store = S3ObjectStore(settings)
    provider = ProviderGate(settings)
    await store.ensure_bucket()
    try:
        async with database.session() as session:
            schema_revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            if schema_revision != "0006_bound_cost_reservations":
                raise RuntimeError("database is not at the frozen measurement schema head")
            active_jobs = (
                await session.scalar(
                    select(func.count())
                    .select_from(IngestionJob)
                    .where(IngestionJob.is_active.is_(True))
                )
                or 0
            )
            if active_jobs:
                raise RuntimeError("pilot requires no pre-existing active ingestion jobs")
            availability = await provider.require_available(session)
            # The pilot measures ingestion, which runs on PAGEINDEX_MODEL, so its snapshot
            # records that model's rates rather than the query model's.
            prices = {
                "input_per_million_usd": settings.pageindex_input_cost_per_million_usd,
                "cache_hit_input_per_million_usd": (
                    settings.pageindex_cache_hit_input_cost_per_million_usd
                ),
                "output_per_million_usd": settings.pageindex_output_cost_per_million_usd,
                "max_input_tokens": DEEPSEEK_MAX_INPUT_TOKENS,
                "max_output_tokens": settings.pageindex_max_output_tokens,
                "max_attempts": settings.pageindex_llm_max_attempts,
            }
            if any(value is None for value in prices.values()):
                raise RuntimeError("pilot requires a complete provider price snapshot")
            configuration_hash = configuration_digest(
                {
                    "provider": provider_configuration_hash(settings),
                    "pageindex_version": settings.pageindex_version,
                    "metadata_prompt_version": settings.metadata_prompt_version,
                    "async_concurrency": settings.pageindex_async_concurrency,
                }
            )
            experiment = Experiment(
                name=f"{manifest.name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                release_sha=release_sha,
                configuration_hash=configuration_hash,
                corpus_hash=manifest.corpus_snapshot_sha256,
                index_hash=configuration_hash,
                dataset_hash=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                price_snapshot=prices,
                repetitions=1,
                evidence_token_budget=settings.evidence_token_limit,
                seed=20_260_725,
                maximum_cost_usd=Decimal(str(settings.pilot_cost_limit_usd)),
                frozen_at=datetime.now(UTC),
            )
            session.add(experiment)
            await session.flush()
            experiment_id = experiment.id
            checked_at = availability.checked_at

        results: list[dict[str, object]] = []
        for sequence, document in enumerate(manifest.documents):
            path = settings.arxiv_pdf_dir / f"{document.arxiv_id}.pdf"
            async with database.session() as session:
                _, job, _ = await register_pdf(
                    session,
                    store,
                    path.read_bytes(),
                    path.name,
                    settings,
                    force=True,
                    mode=IngestionMode.reextract_all,
                    experiment_id=experiment_id,
                )
                if job is None:
                    raise RuntimeError("pilot rebuild did not create an ingestion job")
                job_id = job.id
            started = time.monotonic()
            job = await wait_for_job(database, job_id)
            row: dict[str, object] = {
                "sequence": sequence,
                "arxiv_id": document.arxiv_id,
                "job_id": str(job.id),
                "status": job.status.value,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "index_version_id": (
                    str(job.active_index_version_id)
                    if job.active_index_version_id is not None
                    else None
                ),
            }
            results.append(row)
            if job.status != JobStatus.completed or job.active_index_version_id is None:
                break
            async with database.session() as session:
                session.add(
                    ExperimentIndexVersion(
                        experiment_id=experiment_id,
                        index_version_id=job.active_index_version_id,
                    )
                )

        async with database.session() as session:
            reservations = list(
                (
                    await session.scalars(
                        select(ModelCallCostReservation).where(
                            ModelCallCostReservation.experiment_id == experiment_id
                        )
                    )
                ).all()
            )
        committed = sum(
            (
                reservation.measured_cost_usd
                if reservation.status == "finalized" and reservation.measured_cost_usd is not None
                else reservation.reserved_cost_usd
            )
            for reservation in reservations
            if reservation.status != "released"
        )
        return {
            "schema_version": 1,
            "experiment_id": str(experiment_id),
            "release_sha": release_sha,
            "provider_checked_at": checked_at.isoformat(),
            "configuration_hash": configuration_hash,
            "manifest_hash": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            "documents": results,
            "reservation_count": len(reservations),
            "measured_or_reserved_cost_usd": report_cost(committed),
            "cost_cap_usd": settings.pilot_cost_limit_usd,
            "retrieval_canaries": "pending_reviewed_evaluation_cases",
        }
    finally:
        await database.close()


def reserve_output(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=False)
    temporary = path.parent / f".{path.name}.tmp"
    temporary.touch(exist_ok=False)
    return temporary


def main() -> None:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "pilots" / "pageindex-v2-25.json",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-cost-cap", type=float)
    parser.add_argument("--release-sha")
    parser.add_argument("--output", default=str(root / "pilots" / "run" / "result.json"))
    args = parser.parse_args()
    load_pilot_manifest(args.manifest)
    if not args.apply:
        print(
            json.dumps({"manifest": str(args.manifest), "valid": True, "applied": False}, indent=2)
        )
        return
    if args.confirm_cost_cap != 10:
        raise SystemExit("--apply requires --confirm-cost-cap 10")
    if args.release_sha is None:
        raise SystemExit("--apply requires --release-sha")
    output = None if args.output == "-" else Path(args.output)
    temporary = reserve_output(output) if output is not None else None
    result = asyncio.run(run(args))
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output is not None and temporary is not None:
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(output)
    print(serialized, end="")


if __name__ == "__main__":
    main()
