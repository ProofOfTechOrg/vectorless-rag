"""Add provider state, frozen experiments, call diagnostics, and classifications."""

# pyright: reportUnknownArgumentType=false

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_measurement_experiments"
down_revision: str | None = "0004_pageindex_artifact_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE agent.model_call_outcome ADD VALUE IF NOT EXISTS 'cancelled'")
        op.execute("ALTER TYPE agent.model_call_outcome ADD VALUE IF NOT EXISTS 'abandoned'")
        op.execute(
            "ALTER TYPE agent.run_failure_code ADD VALUE IF NOT EXISTS 'provider_unavailable'"
        )
        op.execute(
            "ALTER TYPE agent.run_failure_code ADD VALUE IF NOT EXISTS 'upstream_invalid_response'"
        )
    op.execute(
        "CREATE TYPE agent.provider_availability AS ENUM "
        "('available','unavailable','unknown','unconfigured')"
    )

    op.create_table(
        "provider_states",
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column(
            "availability",
            postgresql.ENUM(name="provider_availability", schema="agent", create_type=False),
            nullable=False,
        ),
        sa.Column("reason", sa.String(80)),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_available_at", sa.DateTime(timezone=True)),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            """
            (availability::text = 'available' AND reason IS NULL)
            OR
            (availability::text <> 'available' AND reason IS NOT NULL)
            """,
            name="ck_provider_state_reason",
        ),
        schema="agent",
    )

    op.create_table(
        "experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("release_sha", sa.String(64), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("corpus_hash", sa.String(64), nullable=False),
        sa.Column("index_hash", sa.String(64), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.Column("price_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("repetitions", sa.Integer, nullable=False),
        sa.Column("evidence_token_budget", sa.Integer, nullable=False),
        sa.Column("seed", sa.BigInteger, nullable=False),
        sa.Column("maximum_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("valid", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("invalid_reason", sa.String(80)),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("repetitions > 0", name="ck_experiment_repetitions"),
        sa.CheckConstraint("evidence_token_budget > 0", name="ck_experiment_evidence_budget"),
        sa.CheckConstraint("maximum_cost_usd > 0", name="ck_experiment_cost_cap"),
        sa.CheckConstraint(
            "(valid AND invalid_reason IS NULL) OR (NOT valid AND invalid_reason IS NOT NULL)",
            name="ck_experiment_validity",
        ),
        schema="agent",
    )
    op.create_table(
        "experiment_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.experiments.id"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_attempts.id"),
        ),
        sa.Column(
            "query_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.query_runs.id"),
        ),
        sa.Column("dataset_item_id", postgresql.UUID(as_uuid=True)),
        sa.Column("trial_key", sa.String(128), nullable=False),
        sa.Column("repetition", sa.Integer, nullable=False),
        sa.Column("sequence_number", sa.Integer, nullable=False),
        sa.Column("included", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(attempt_id IS NULL) <> (query_run_id IS NULL)",
            name="ck_experiment_membership_one_owner",
        ),
        sa.CheckConstraint("repetition > 0", name="ck_experiment_membership_repetition"),
        sa.CheckConstraint("sequence_number >= 0", name="ck_experiment_membership_sequence"),
        schema="agent",
    )
    op.create_index(
        "uq_experiment_attempt_membership",
        "experiment_memberships",
        ["experiment_id", "attempt_id"],
        unique=True,
        schema="agent",
        postgresql_where=sa.text("attempt_id IS NOT NULL"),
    )
    op.create_index(
        "uq_experiment_query_trial_membership",
        "experiment_memberships",
        ["experiment_id", "query_run_id", "trial_key"],
        unique=True,
        schema="agent",
        postgresql_where=sa.text("query_run_id IS NOT NULL"),
    )
    op.create_table(
        "experiment_index_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.experiments.id"),
            nullable=False,
        ),
        sa.Column(
            "index_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.index_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "index_version_id",
            name="uq_experiment_index_version",
        ),
        schema="agent",
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.experiments.id"),
        ),
        schema="agent",
    )

    op.create_table(
        "model_call_cost_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.experiments.id"),
            nullable=False,
        ),
        sa.Column("logical_call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reserved_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("measured_cost_usd", sa.Numeric(14, 6)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "experiment_id", "logical_call_id", name="uq_cost_reservation_logical_call"
        ),
        sa.CheckConstraint(
            "reserved_cost_usd >= 0 AND (measured_cost_usd IS NULL OR measured_cost_usd >= 0)",
            name="ck_cost_reservation_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','finalized','review_required','released')",
            name="ck_cost_reservation_status",
        ),
        sa.CheckConstraint(
            "(status = 'finalized' AND measured_cost_usd IS NOT NULL) OR (status <> 'finalized')",
            name="ck_cost_reservation_finalized",
        ),
        schema="agent",
    )

    op.add_column(
        "model_calls",
        sa.Column("logical_call_id", postgresql.UUID(as_uuid=True)),
        schema="agent",
    )
    op.add_column("model_calls", sa.Column("operation", sa.String(80)), schema="agent")
    op.add_column("model_calls", sa.Column("http_status", sa.Integer), schema="agent")
    op.add_column("model_calls", sa.Column("failure_code", sa.String(80)), schema="agent")
    op.add_column("model_calls", sa.Column("retryable", sa.Boolean), schema="agent")
    op.add_column("model_calls", sa.Column("latency_ms", sa.Integer), schema="agent")
    op.add_column("model_calls", sa.Column("retry_delay_ms", sa.Integer), schema="agent")
    op.add_column("model_calls", sa.Column("prompt_sha256", sa.String(64)), schema="agent")
    op.add_column("model_calls", sa.Column("prompt_bytes", sa.BigInteger), schema="agent")
    op.add_column("model_calls", sa.Column("output_sha256", sa.String(64)), schema="agent")
    op.add_column("model_calls", sa.Column("output_bytes", sa.BigInteger), schema="agent")
    op.add_column(
        "model_calls",
        sa.Column("cancelled", sa.Boolean, server_default=sa.false(), nullable=False),
        schema="agent",
    )
    op.add_column("model_calls", sa.Column("release", sa.String(128)), schema="agent")
    op.add_column("model_calls", sa.Column("configuration_hash", sa.String(64)), schema="agent")
    op.add_column(
        "model_calls",
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.experiments.id"),
        ),
        schema="agent",
    )
    op.add_column(
        "model_calls",
        sa.Column(
            "cost_reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.model_call_cost_reservations.id"),
        ),
        schema="agent",
    )
    op.execute(
        """
        UPDATE agent.model_calls
        SET logical_call_id = id,
            operation = stage::text
        """
    )
    op.alter_column("model_calls", "logical_call_id", nullable=False, schema="agent")
    op.alter_column("model_calls", "operation", nullable=False, schema="agent")
    op.create_index(
        "ix_model_calls_logical_call", "model_calls", ["logical_call_id"], schema="agent"
    )
    op.create_check_constraint(
        "ck_model_call_http_status",
        "model_calls",
        "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
        schema="agent",
    )
    op.create_check_constraint(
        "ck_model_call_diagnostics",
        "model_calls",
        "(latency_ms IS NULL OR latency_ms >= 0) AND "
        "(retry_delay_ms IS NULL OR retry_delay_ms >= 0) AND "
        "(prompt_bytes IS NULL OR prompt_bytes >= 0) AND "
        "(output_bytes IS NULL OR output_bytes >= 0)",
        schema="agent",
    )

    for name, type_, default in (
        ("strategy", sa.String(80), "pageindex"),
        ("strategy_version", sa.String(128), None),
        ("configuration_hash", sa.String(64), None),
        ("index_digests", postgresql.JSONB, sa.text("'{}'::jsonb")),
        ("ranked_candidates", postgresql.JSONB, sa.text("'[]'::jsonb")),
        ("node_choices", postgresql.JSONB, sa.text("'[]'::jsonb")),
        ("evidence_tokens", sa.Integer, "0"),
        ("unique_pages", sa.Integer, "0"),
        ("overlapping_pages_avoided", sa.Integer, "0"),
        ("merged_ranges", postgresql.JSONB, sa.text("'[]'::jsonb")),
        ("content_truncated", sa.Boolean, sa.false()),
        ("progress", postgresql.JSONB, sa.text("'[]'::jsonb")),
        ("phase_timings_ms", postgresql.JSONB, sa.text("'{}'::jsonb")),
    ):
        op.add_column(
            "retrieval_audits",
            sa.Column(
                name,
                type_,
                server_default=default,
                nullable=default is None,
            ),
            schema="agent",
        )
    op.create_check_constraint(
        "ck_retrieval_expanded_counts",
        "retrieval_audits",
        "evidence_tokens >= 0 AND unique_pages >= 0 AND overlapping_pages_avoided >= 0",
        schema="agent",
    )

    op.create_table(
        "measurement_classification_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ingestion_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_attempts.id"),
        ),
        sa.Column(
            "query_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.query_runs.id"),
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column(
            "replacement_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_attempts.id"),
        ),
        sa.Column(
            "prior_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.measurement_classification_events.id"),
        ),
        sa.Column("justification", sa.Text, nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(ingestion_attempt_id IS NULL) <> (query_run_id IS NULL)",
            name="ck_classification_one_owner",
        ),
        sa.CheckConstraint("action IN ('exclude','reinstate')", name="ck_classification_action"),
        sa.CheckConstraint(
            "reason IN ("
            "'insufficient_credit','pre_freeze_code_bug','manual_review',"
            "'invalid_experiment','legacy_missing_telemetry'"
            ")",
            name="ck_classification_reason",
        ),
        sa.CheckConstraint(
            "length(justification) BETWEEN 1 AND 2000",
            name="ck_classification_justification",
        ),
        schema="agent",
    )
    op.create_index(
        "ix_classification_attempt",
        "measurement_classification_events",
        ["ingestion_attempt_id", "created_at"],
        schema="agent",
    )
    op.execute(
        """
        CREATE FUNCTION agent.classification_events_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'measurement classifications are append only';
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER classification_events_append_only_trigger
        BEFORE UPDATE OR DELETE ON agent.measurement_classification_events
        FOR EACH ROW EXECUTE FUNCTION agent.classification_events_append_only();

        CREATE FUNCTION agent.experiment_identity_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.name IS DISTINCT FROM OLD.name OR
             NEW.release_sha IS DISTINCT FROM OLD.release_sha OR
             NEW.configuration_hash IS DISTINCT FROM OLD.configuration_hash OR
             NEW.corpus_hash IS DISTINCT FROM OLD.corpus_hash OR
             NEW.index_hash IS DISTINCT FROM OLD.index_hash OR
             NEW.dataset_hash IS DISTINCT FROM OLD.dataset_hash OR
             NEW.price_snapshot IS DISTINCT FROM OLD.price_snapshot OR
             NEW.repetitions IS DISTINCT FROM OLD.repetitions OR
             NEW.evidence_token_budget IS DISTINCT FROM OLD.evidence_token_budget OR
             NEW.seed IS DISTINCT FROM OLD.seed OR
             NEW.maximum_cost_usd IS DISTINCT FROM OLD.maximum_cost_usd OR
             NEW.frozen_at IS DISTINCT FROM OLD.frozen_at THEN
            RAISE EXCEPTION 'frozen experiment identity is immutable';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER experiment_identity_immutable_trigger
        BEFORE UPDATE ON agent.experiments
        FOR EACH ROW EXECUTE FUNCTION agent.experiment_identity_immutable();

        CREATE FUNCTION agent.memberships_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'experiment membership is append only';
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER memberships_append_only_trigger
        BEFORE UPDATE OR DELETE ON agent.experiment_memberships
        FOR EACH ROW EXECUTE FUNCTION agent.memberships_append_only();

        CREATE TRIGGER experiment_index_versions_append_only_trigger
        BEFORE UPDATE OR DELETE ON agent.experiment_index_versions
        FOR EACH ROW EXECUTE FUNCTION agent.memberships_append_only();

        CREATE FUNCTION agent.enforce_experiment_cost_cap() RETURNS trigger AS $$
        DECLARE
          cap numeric(14,6);
          committed numeric(14,6);
        BEGIN
          SELECT maximum_cost_usd INTO cap
          FROM agent.experiments
          WHERE id = NEW.experiment_id
          FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'experiment does not exist';
          END IF;
          SELECT coalesce(sum(
            CASE
              WHEN status = 'released' THEN 0
              WHEN status = 'finalized' THEN measured_cost_usd
              ELSE reserved_cost_usd
            END
          ), 0)
          INTO committed
          FROM agent.model_call_cost_reservations
          WHERE experiment_id = NEW.experiment_id;
          IF committed + NEW.reserved_cost_usd > cap THEN
            RAISE EXCEPTION 'experiment cost cap exceeded';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER enforce_experiment_cost_cap_trigger
        BEFORE INSERT ON agent.model_call_cost_reservations
        FOR EACH ROW EXECUTE FUNCTION agent.enforce_experiment_cost_cap();

        CREATE VIEW agent.effective_measurement_classifications
        WITH (security_barrier=true) AS
        WITH ranked AS (
          SELECT e.*,
            row_number() OVER (
              PARTITION BY e.ingestion_attempt_id, e.query_run_id
              ORDER BY e.created_at DESC, e.id DESC
            ) AS position
          FROM agent.measurement_classification_events e
        )
        SELECT ingestion_attempt_id, query_run_id,
               action, reason, replacement_attempt_id, created_at
        FROM ranked
        WHERE position = 1;
        """
    )

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
            GRANT SELECT, INSERT, UPDATE ON agent.provider_states,
              agent.experiments, agent.experiment_memberships,
              agent.experiment_index_versions,
              agent.model_call_cost_reservations TO rag_app;
            GRANT SELECT ON agent.effective_measurement_classifications TO rag_app;
            REVOKE ALL ON agent.measurement_classification_events FROM rag_app;
            REVOKE DELETE ON agent.ingestion_attempts, agent.model_calls,
              agent.query_runs, agent.retrieval_audits, agent.sql_audits,
              agent.experiments, agent.experiment_memberships,
              agent.experiment_index_versions,
              agent.model_call_cost_reservations, agent.provider_states FROM rag_app;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_reader') THEN
            REVOKE ALL ON agent.provider_states, agent.experiments,
              agent.experiment_memberships, agent.model_call_cost_reservations,
              agent.experiment_index_versions,
              agent.measurement_classification_events FROM rag_reader;
            GRANT SELECT ON agent.effective_measurement_classifications TO rag_reader;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW IF EXISTS agent.effective_measurement_classifications;
        DROP TRIGGER IF EXISTS enforce_experiment_cost_cap_trigger
          ON agent.model_call_cost_reservations;
        DROP FUNCTION IF EXISTS agent.enforce_experiment_cost_cap();
        DROP TRIGGER IF EXISTS memberships_append_only_trigger
          ON agent.experiment_memberships;
        DROP TRIGGER IF EXISTS experiment_index_versions_append_only_trigger
          ON agent.experiment_index_versions;
        DROP FUNCTION IF EXISTS agent.memberships_append_only();
        DROP TRIGGER IF EXISTS experiment_identity_immutable_trigger ON agent.experiments;
        DROP FUNCTION IF EXISTS agent.experiment_identity_immutable();
        DROP TRIGGER IF EXISTS classification_events_append_only_trigger
          ON agent.measurement_classification_events;
        DROP FUNCTION IF EXISTS agent.classification_events_append_only();
        """
    )
    op.drop_table("measurement_classification_events", schema="agent")
    op.drop_constraint(
        "ck_retrieval_expanded_counts", "retrieval_audits", schema="agent", type_="check"
    )
    for column in (
        "phase_timings_ms",
        "progress",
        "content_truncated",
        "merged_ranges",
        "overlapping_pages_avoided",
        "unique_pages",
        "evidence_tokens",
        "node_choices",
        "ranked_candidates",
        "index_digests",
        "configuration_hash",
        "strategy_version",
        "strategy",
    ):
        op.drop_column("retrieval_audits", column, schema="agent")
    op.drop_constraint("ck_model_call_diagnostics", "model_calls", schema="agent", type_="check")
    op.drop_constraint("ck_model_call_http_status", "model_calls", schema="agent", type_="check")
    op.drop_index("ix_model_calls_logical_call", table_name="model_calls", schema="agent")
    for column in (
        "cost_reservation_id",
        "experiment_id",
        "configuration_hash",
        "release",
        "cancelled",
        "output_bytes",
        "output_sha256",
        "prompt_bytes",
        "prompt_sha256",
        "retry_delay_ms",
        "latency_ms",
        "retryable",
        "failure_code",
        "http_status",
        "operation",
        "logical_call_id",
    ):
        op.drop_column("model_calls", column, schema="agent")
    op.drop_table("model_call_cost_reservations", schema="agent")
    op.drop_column("ingestion_jobs", "experiment_id", schema="agent")
    op.drop_table("experiment_index_versions", schema="agent")
    op.drop_index(
        "uq_experiment_query_trial_membership",
        table_name="experiment_memberships",
        schema="agent",
    )
    op.drop_index(
        "uq_experiment_attempt_membership",
        table_name="experiment_memberships",
        schema="agent",
    )
    op.drop_table("experiment_memberships", schema="agent")
    op.drop_table("experiments", schema="agent")
    op.drop_table("provider_states", schema="agent")
    op.execute("DROP TYPE agent.provider_availability")
