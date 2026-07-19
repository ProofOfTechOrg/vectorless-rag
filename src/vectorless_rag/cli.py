from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from vectorless_rag.auth import generate_api_key, hash_api_key, validate_scopes
from vectorless_rag.config import get_settings
from vectorless_rag.db import Database
from vectorless_rag.ingestion import build_pilot_report
from vectorless_rag.models import ApiKey, IngestionJob

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
                jobs = list(
                    (
                        await session.scalars(
                            select(IngestionJob).order_by(IngestionJob.created_at).limit(10)
                        )
                    ).all()
                )
                return build_pilot_report(
                    jobs,
                    input_cost_per_million_usd=settings.deepseek_input_cost_per_million_usd,
                    output_cost_per_million_usd=settings.deepseek_output_cost_per_million_usd,
                )
        finally:
            await database.close()

    typer.echo(json.dumps(asyncio.run(report()), indent=2))


if __name__ == "__main__":
    app()
