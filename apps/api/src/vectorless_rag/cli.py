from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from vectorless_rag.artifact_ops import (
    apply_gc_plan,
    build_gc_plan,
    lock_artifact_mutations,
    verify_and_activate_index_version,
    verify_index_version,
)
from vectorless_rag.auth import generate_api_key, hash_api_key, validate_scopes
from vectorless_rag.config import get_settings
from vectorless_rag.db import Database
from vectorless_rag.models import (
    ApiKey,
    Document,
    IndexVersion,
    IngestionAttempt,
    IngestionJob,
    ModelCall,
    QueryRun,
)
from vectorless_rag.reporting import (
    build_pilot_report,
    build_query_cost_report,
    indexing_pricing,
    query_pricing,
    scan_corpus,
)
from vectorless_rag.storage import S3ObjectStore
from vectorless_rag.telemetry_classification import (
    classification_report,
    read_classification_manifest,
)

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


@app.command("migrate")
def migrate() -> None:
    settings = get_settings()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_admin_url)
    command.upgrade(config, "head")


@app.command("bootstrap-key")
def bootstrap_key(
    name: Annotated[str, typer.Option(prompt=True)],
    scopes: Annotated[str, typer.Option()] = "chat,documents:read,admin:ingest",
) -> None:
    """Create an API key; the plaintext is printed once."""

    async def create() -> str:
        settings = get_settings()
        values = {item.strip() for item in scopes.split(",") if item.strip()}
        validate_scopes(values)
        prefix, raw = generate_api_key()
        database = Database(settings)
        try:
            async with database.session() as session:
                session.add(
                    ApiKey(
                        name=name,
                        prefix=prefix,
                        key_hash=hash_api_key(raw, settings.api_key_pepper.get_secret_value()),
                        scopes=sorted(values),
                    )
                )
            return raw
        finally:
            await database.close()

    typer.echo(asyncio.run(create()))


@app.command("pilot-report")
def pilot_report() -> None:
    async def report() -> dict[str, object]:
        settings = get_settings()
        database = Database(settings)
        try:
            async with database.session() as session:
                documents = list(
                    (
                        await session.scalars(
                            select(Document)
                            .order_by(Document.created_at, Document.id)
                            .limit(settings.pilot_document_limit)
                        )
                    ).all()
                )
                document_ids = [document.id for document in documents]
                jobs = list(
                    (
                        await session.scalars(
                            select(IngestionJob)
                            .where(IngestionJob.document_id.in_(document_ids))
                            .order_by(IngestionJob.created_at, IngestionJob.id)
                        )
                    ).all()
                )
                job_ids = [job.id for job in jobs]
                attempts = list(
                    (
                        await session.scalars(
                            select(IngestionAttempt)
                            .where(IngestionAttempt.job_id.in_(job_ids))
                            .order_by(
                                IngestionAttempt.created_at,
                                IngestionAttempt.id,
                            )
                        )
                    ).all()
                )
                attempt_ids = [attempt.id for attempt in attempts]
                calls = list(
                    (
                        await session.scalars(
                            select(ModelCall)
                            .where(ModelCall.ingestion_attempt_id.in_(attempt_ids))
                            .order_by(ModelCall.created_at, ModelCall.id)
                        )
                    ).all()
                )
                return build_pilot_report(
                    documents,
                    jobs,
                    attempts,
                    calls,
                    scan_corpus(settings.arxiv_pdf_dir),
                    # This report covers ingestion, which runs on the PageIndex model.
                    indexing_pricing(settings),
                )
        finally:
            await database.close()

    typer.echo(json.dumps(asyncio.run(report()), indent=2))


@app.command("query-cost-report")
def query_cost_report(
    days: Annotated[int, typer.Option(min=1)] = 30,
) -> None:
    async def report() -> dict[str, object]:
        settings = get_settings()
        database = Database(settings)
        cutoff = datetime.now(UTC) - timedelta(days=days)
        try:
            async with database.session() as session:
                runs = list(
                    (
                        await session.scalars(
                            select(QueryRun)
                            .where(QueryRun.created_at >= cutoff)
                            .order_by(QueryRun.created_at, QueryRun.id)
                        )
                    ).all()
                )
                run_ids = [run.id for run in runs]
                calls = list(
                    (
                        await session.scalars(
                            select(ModelCall)
                            .where(ModelCall.query_run_id.in_(run_ids))
                            .order_by(ModelCall.created_at, ModelCall.id)
                        )
                    ).all()
                )
                return build_query_cost_report(
                    runs,
                    calls,
                    # This report covers serving, which runs on the query model.
                    query_pricing(settings),
                    days=days,
                )
        finally:
            await database.close()

    typer.echo(json.dumps(asyncio.run(report()), indent=2))


@app.command("artifact-verify")
def artifact_verify(
    version_id: Annotated[uuid.UUID | None, typer.Option()] = None,
) -> None:
    """Verify one or every active PageIndex-v2 manifest and referenced object."""

    async def verify() -> list[dict[str, object]]:
        settings = get_settings()
        database = Database(settings)
        store = S3ObjectStore(settings)
        try:
            async with database.session() as session:
                identifiers = (
                    [version_id]
                    if version_id is not None
                    else list(
                        (
                            await session.scalars(
                                select(IndexVersion.id)
                                .join(
                                    Document,
                                    Document.active_index_version_id == IndexVersion.id,
                                )
                                .where(IndexVersion.artifact_schema_version == 2)
                                .order_by(IndexVersion.document_id)
                            )
                        ).all()
                    )
                )
                return [
                    dataclasses.asdict(await verify_index_version(session, store, identifier))
                    for identifier in identifiers
                ]
        finally:
            await database.close()

    typer.echo(json.dumps(asyncio.run(verify()), indent=2, default=str))


@app.command("artifact-rollback")
def artifact_rollback(
    document_id: Annotated[uuid.UUID, typer.Option()],
    version_id: Annotated[uuid.UUID, typer.Option()],
    actor: Annotated[str, typer.Option()] = "operator",
) -> None:
    """Atomically activate a verified prior index version."""

    async def rollback() -> None:
        settings = get_settings()
        database = Database(settings)
        store = S3ObjectStore(settings)
        try:
            async with database.session() as session:
                await verify_and_activate_index_version(
                    session,
                    store,
                    document_id=document_id,
                    version_id=version_id,
                    actor=actor,
                )
        finally:
            await database.close()

    asyncio.run(rollback())
    typer.echo(json.dumps({"document_id": str(document_id), "version_id": str(version_id)}))


@app.command("artifact-gc")
def artifact_gc(
    apply: Annotated[bool, typer.Option()] = False,
    confirm_count: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Plan v2 object cleanup; deletion requires --apply and an exact count."""

    async def collect() -> dict[str, object]:
        settings = get_settings()
        database = Database(settings)
        store = S3ObjectStore(settings)
        try:
            async with database.session() as session:
                if apply:
                    await lock_artifact_mutations(session)
                plan = await build_gc_plan(session, store)
                if apply:
                    if confirm_count != len(plan.delete_keys):
                        count = len(plan.delete_keys)
                        raise ValueError(f"--confirm-count must equal the dry-run count ({count})")
                    await apply_gc_plan(store, plan)
            result = dataclasses.asdict(plan)
            result["applied"] = apply
            return result
        finally:
            await database.close()

    try:
        result = asyncio.run(collect())
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("telemetry-classify")
def telemetry_classify(
    manifest: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
    apply: Annotated[bool, typer.Option()] = False,
    confirm_count: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Validate or append an exact measurement-classification manifest."""
    parsed = read_classification_manifest(manifest)
    if apply and confirm_count != len(parsed.entries):
        raise typer.BadParameter(
            f"--confirm-count must equal the manifest count ({len(parsed.entries)})"
        )
    settings = get_settings()
    result = asyncio.run(
        classification_report(
            settings,
            manifest=parsed,
            apply=apply,
        )
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("telemetry-classifications")
def telemetry_classifications() -> None:
    """Report raw totals and effective append-only classifications."""
    result = asyncio.run(classification_report(get_settings()))
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
