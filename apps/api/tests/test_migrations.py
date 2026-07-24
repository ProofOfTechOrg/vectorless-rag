from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic.config import Config

from alembic import command

API_ROOT = Path(__file__).resolve().parents[1]


def test_usage_migration_preserves_timeout_cancellation_and_generic_failures() -> None:
    output = StringIO()
    config = Config(str(API_ROOT / "alembic.ini"), output_buffer=output)
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    command.upgrade(config, "head", sql=True)
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
