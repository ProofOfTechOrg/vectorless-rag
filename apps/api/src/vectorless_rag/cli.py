from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from vectorless_rag.auth import generate_api_key, hash_api_key, validate_scopes
from vectorless_rag.config import get_settings
from vectorless_rag.db import Database
from vectorless_rag.models import (
    ApiKey,
    Document,
    IngestionAttempt,
    IngestionJob,
    ModelCall,
    QueryRun,
)
from vectorless_rag.reporting import (
    Pricing,
    build_pilot_report,
    build_query_cost_report,
    scan_corpus,
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
                    Pricing(
                        settings.deepseek_input_cost_per_million_usd,
                        settings.deepseek_cache_hit_input_cost_per_million_usd,
                        settings.deepseek_output_cost_per_million_usd,
                    ),
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
                    Pricing(
                        settings.deepseek_input_cost_per_million_usd,
                        settings.deepseek_cache_hit_input_cost_per_million_usd,
                        settings.deepseek_output_cost_per_million_usd,
                    ),
                    days=days,
                )
        finally:
            await database.close()

    typer.echo(json.dumps(asyncio.run(report()), indent=2))


if __name__ == "__main__":
    app()
