from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

API_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(output_buffer: StringIO | None = None) -> Config:
    config = Config(str(API_ROOT / "alembic.ini"), output_buffer=output_buffer)
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


def test_revision_ids_fit_alembic_version_column() -> None:
    revisions = ScriptDirectory.from_config(alembic_config()).walk_revisions()

    assert all(len(revision.revision) <= 32 for revision in revisions)


def test_usage_migration_preserves_timeout_cancellation_and_generic_failures() -> None:
    output = StringIO()
    command.upgrade(alembic_config(output), "head", sql=True)
    sql = output.getvalue()

    assert (
        "WHEN error = 'request deadline exceeded'\n"
        "                THEN 'deadline_exceeded'::agent.run_failure_code"
    ) in sql
    assert (
        "WHEN error = 'client cancelled request'\n"
        "                THEN 'client_cancelled'::agent.run_failure_code"
    ) in sql
    assert "ELSE 'request_failed'::agent.run_failure_code" in sql
    assert ("WHEN error = 'client cancelled request' THEN 'client cancelled request'") in sql
    assert "ELSE 'request failed'" in sql
