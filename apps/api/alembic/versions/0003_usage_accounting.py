"""Add durable model-call accounting and sanitize historical query failures.

The downgrade intentionally cannot restore exception text removed from query_runs.error.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_usage_accounting"
down_revision: str | None = "0002_add_help_run_route"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

attempt_status = postgresql.ENUM(
    "running",
    "completed",
    "failed",
    "unsupported",
    "abandoned",
    name="ingestion_attempt_status",
    schema="agent",
    create_type=False,
)
artifact_action = postgresql.ENUM(
    "created",
    "rebuilt",
    "reused",
    "none",
    name="artifact_action",
    schema="agent",
    create_type=False,
)
model_call_outcome = postgresql.ENUM(
    "started",
    "completed",
    "failed",
    name="model_call_outcome",
    schema="agent",
    create_type=False,
)
model_call_stage = postgresql.ENUM(
    "ingestion_metadata",
    "pageindex_generation",
    "query_route",
    "query_sql",
    "query_document_select",
    "query_node_select",
    "query_sufficiency",
    "query_synthesis",
    name="model_call_stage",
    schema="agent",
    create_type=False,
)
run_failure_code = postgresql.ENUM(
    "deadline_exceeded",
    "client_cancelled",
    "request_failed",
    "usage_persistence_failed",
    name="run_failure_code",
    schema="agent",
    create_type=False,
)


def timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.execute(
        "CREATE TYPE agent.ingestion_attempt_status AS ENUM "
        "('running','completed','failed','unsupported','abandoned')"
    )
    op.execute("CREATE TYPE agent.artifact_action AS ENUM ('created','rebuilt','reused','none')")
    op.execute("CREATE TYPE agent.model_call_outcome AS ENUM ('started','completed','failed')")
    op.execute(
        "CREATE TYPE agent.model_call_stage AS ENUM "
        "('ingestion_metadata','pageindex_generation','query_route','query_sql',"
        "'query_document_select','query_node_select','query_sufficiency','query_synthesis')"
    )
    op.execute(
        "CREATE TYPE agent.run_failure_code AS ENUM "
        "('deadline_exceeded','client_cancelled','request_failed','usage_persistence_failed')"
    )

    op.create_table(
        "ingestion_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_jobs.id"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("trace_id", sa.String(64), unique=True, nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column[Any]("status", attempt_status, server_default="running", nullable=False),
        sa.Column("failure_code", sa.String(80)),
        sa.Column[Any]("artifact_action", artifact_action, server_default="none", nullable=False),
        sa.Column("page_count", sa.Integer),
        sa.Column("extraction_quality", sa.Float()),
        sa.Column("artifact_bytes", sa.BigInteger()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("elapsed_seconds", sa.Float()),
        sa.Column("telemetry_complete", sa.Boolean, server_default=sa.false(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_ingestion_attempt_job_number"),
        sa.CheckConstraint("attempt_number > 0", name="ck_ingestion_attempt_number_positive"),
        sa.CheckConstraint("page_count IS NULL OR page_count >= 0", name="ck_attempt_page_count"),
        sa.CheckConstraint(
            "artifact_bytes IS NULL OR artifact_bytes >= 0", name="ck_attempt_artifact_bytes"
        ),
        sa.CheckConstraint(
            "elapsed_seconds IS NULL OR elapsed_seconds >= 0", name="ck_attempt_elapsed"
        ),
        schema="agent",
    )

    op.add_column(
        "query_runs",
        sa.Column[Any]("failure_code", run_failure_code),
        schema="agent",
    )
    op.execute(
        """
        UPDATE agent.query_runs
        SET failure_code = CASE
              WHEN error = 'request deadline exceeded'
                THEN 'deadline_exceeded'::agent.run_failure_code
              WHEN error = 'client cancelled request'
                THEN 'client_cancelled'::agent.run_failure_code
              ELSE 'request_failed'::agent.run_failure_code
            END,
            error = CASE
              WHEN error = 'request deadline exceeded' THEN 'request deadline exceeded'
              WHEN error = 'client cancelled request' THEN 'client cancelled request'
              ELSE 'request failed'
            END
        WHERE error IS NOT NULL
        """
    )

    op.create_table(
        "model_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "query_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.query_runs.id"),
        ),
        sa.Column(
            "ingestion_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_attempts.id"),
        ),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column[Any]("stage", model_call_stage, nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("provider_attempt", sa.Integer, nullable=False),
        sa.Column("finish_reason", sa.String(80)),
        sa.Column[Any]("outcome", model_call_outcome, server_default="started", nullable=False),
        sa.Column("input_tokens", sa.BigInteger),
        sa.Column("cache_hit_input_tokens", sa.BigInteger),
        sa.Column("cache_miss_input_tokens", sa.BigInteger),
        sa.Column("cache_write_input_tokens", sa.BigInteger),
        sa.Column("unattributed_input_tokens", sa.BigInteger),
        sa.Column("output_tokens", sa.BigInteger),
        sa.Column("reasoning_output_tokens", sa.BigInteger),
        sa.Column("total_tokens", sa.BigInteger),
        sa.Column("usage_available", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column(
            "cache_attribution_complete", sa.Boolean, server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.CheckConstraint(
            "(query_run_id IS NULL) <> (ingestion_attempt_id IS NULL)",
            name="ck_model_call_exactly_one_owner",
        ),
        sa.CheckConstraint("provider_attempt > 0", name="ck_model_call_attempt_positive"),
        sa.CheckConstraint(
            """
            (NOT usage_available AND
             input_tokens IS NULL AND cache_hit_input_tokens IS NULL AND
             cache_miss_input_tokens IS NULL AND cache_write_input_tokens IS NULL AND
             unattributed_input_tokens IS NULL AND output_tokens IS NULL AND
             reasoning_output_tokens IS NULL AND total_tokens IS NULL)
            OR
            (usage_available AND
             input_tokens IS NOT NULL AND cache_hit_input_tokens IS NOT NULL AND
             cache_miss_input_tokens IS NOT NULL AND cache_write_input_tokens IS NOT NULL AND
             unattributed_input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND
             reasoning_output_tokens IS NOT NULL AND total_tokens IS NOT NULL AND
             input_tokens >= 0 AND cache_hit_input_tokens >= 0 AND
             cache_miss_input_tokens >= 0 AND cache_write_input_tokens >= 0 AND
             unattributed_input_tokens >= 0 AND output_tokens >= 0 AND
             reasoning_output_tokens >= 0 AND total_tokens >= 0)
            """,
            name="ck_model_call_usage_availability",
        ),
        sa.CheckConstraint(
            "NOT usage_available OR input_tokens = "
            "cache_hit_input_tokens + cache_miss_input_tokens + unattributed_input_tokens",
            name="ck_model_call_input_partition",
        ),
        sa.CheckConstraint(
            "NOT usage_available OR cache_write_input_tokens <= cache_miss_input_tokens",
            name="ck_model_call_cache_write_subset",
        ),
        sa.CheckConstraint(
            "NOT usage_available OR reasoning_output_tokens <= output_tokens",
            name="ck_model_call_reasoning_subset",
        ),
        sa.CheckConstraint(
            "NOT usage_available OR total_tokens = input_tokens + output_tokens",
            name="ck_model_call_total_partition",
        ),
        sa.CheckConstraint(
            "NOT cache_attribution_complete OR (usage_available AND unattributed_input_tokens = 0)",
            name="ck_model_call_cache_attribution",
        ),
        schema="agent",
    )
    op.create_index("ix_model_calls_query_owner", "model_calls", ["query_run_id"], schema="agent")
    op.create_index(
        "ix_model_calls_ingestion_owner",
        "model_calls",
        ["ingestion_attempt_id"],
        schema="agent",
    )
    op.create_index("ix_model_calls_trace", "model_calls", ["trace_id"], schema="agent")

    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE
              ON agent.ingestion_attempts, agent.model_calls TO rag_app;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_reader') THEN
            REVOKE ALL ON agent.ingestion_attempts, agent.model_calls FROM rag_reader;
          END IF;
        END $$"""
    )


def downgrade() -> None:
    op.drop_index("ix_model_calls_trace", table_name="model_calls", schema="agent")
    op.drop_index("ix_model_calls_ingestion_owner", table_name="model_calls", schema="agent")
    op.drop_index("ix_model_calls_query_owner", table_name="model_calls", schema="agent")
    op.drop_table("model_calls", schema="agent")
    op.drop_column("query_runs", "failure_code", schema="agent")
    op.drop_table("ingestion_attempts", schema="agent")
    op.execute("DROP TYPE agent.run_failure_code")
    op.execute("DROP TYPE agent.model_call_stage")
    op.execute("DROP TYPE agent.model_call_outcome")
    op.execute("DROP TYPE agent.artifact_action")
    op.execute("DROP TYPE agent.ingestion_attempt_status")
