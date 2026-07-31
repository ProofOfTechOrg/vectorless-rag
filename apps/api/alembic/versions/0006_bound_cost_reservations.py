"""Bound model call reservations to the request admitted by the client."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_bound_cost_reservations"
down_revision: str | None = "0005_measurement_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, type_ in (
        ("input_token_limit", sa.BigInteger()),
        ("output_token_limit", sa.Integer()),
        ("attempt_limit", sa.Integer()),
    ):
        op.add_column(
            "model_call_cost_reservations",
            sa.Column(name, type_),
            schema="agent",
        )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            WITH configured_bounds AS (
              SELECT r.id,
                     e.price_snapshot->'max_input_tokens' AS raw_bound,
                     9223372036854775807::numeric AS maximum_bound
              FROM agent.model_call_cost_reservations r
              JOIN agent.experiments e ON e.id = r.experiment_id
              UNION ALL
              SELECT r.id,
                     e.price_snapshot->'max_output_tokens',
                     2147483647::numeric
              FROM agent.model_call_cost_reservations r
              JOIN agent.experiments e ON e.id = r.experiment_id
              UNION ALL
              SELECT r.id,
                     e.price_snapshot->'max_attempts',
                     2147483647::numeric
              FROM agent.model_call_cost_reservations r
              JOIN agent.experiments e ON e.id = r.experiment_id
            )
            SELECT 1
            FROM configured_bounds
            WHERE raw_bound IS NOT NULL
              AND CASE
                WHEN jsonb_typeof(raw_bound) <> 'number' THEN true
                ELSE (raw_bound#>>'{}')::numeric < 1
                  OR (raw_bound#>>'{}')::numeric > maximum_bound
                  OR (raw_bound#>>'{}')::numeric
                     <> trunc((raw_bound#>>'{}')::numeric)
              END
          ) THEN
            RAISE EXCEPTION
              'historical reservation bounds must be positive integral JSON numbers';
          END IF;
        END $$;

        UPDATE agent.model_call_cost_reservations r
        SET input_token_limit = coalesce(
              ((e.price_snapshot->>'max_input_tokens')::numeric)::bigint,
              1000000
            ),
            output_token_limit = coalesce(
              ((e.price_snapshot->>'max_output_tokens')::numeric)::integer,
              384000
            ),
            attempt_limit = coalesce(
              ((e.price_snapshot->>'max_attempts')::numeric)::integer,
              3
            )
        FROM agent.experiments e
        WHERE e.id = r.experiment_id
        """
    )
    for name in ("input_token_limit", "output_token_limit", "attempt_limit"):
        op.alter_column(
            "model_call_cost_reservations",
            name,
            nullable=False,
            schema="agent",
        )
    op.create_check_constraint(
        "ck_cost_reservation_positive_bounds",
        "model_call_cost_reservations",
        "input_token_limit > 0 AND output_token_limit > 0 AND attempt_limit > 0",
        schema="agent",
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1
            FROM agent.model_calls c
            JOIN agent.model_call_cost_reservations r ON r.id = c.cost_reservation_id
            WHERE c.logical_call_id <> r.logical_call_id
               OR c.provider_attempt > r.attempt_limit
               OR (
                 c.query_run_id IS NOT NULL
                 AND NOT EXISTS (
                   SELECT 1
                   FROM agent.experiment_memberships m
                   WHERE m.experiment_id = r.experiment_id
                     AND m.query_run_id = c.query_run_id
                 )
               )
               OR (
                 c.ingestion_attempt_id IS NOT NULL
                 AND NOT EXISTS (
                   SELECT 1
                   FROM agent.ingestion_attempts a
                   JOIN agent.ingestion_jobs j ON j.id = a.job_id
                   WHERE a.id = c.ingestion_attempt_id
                     AND j.experiment_id = r.experiment_id
                 )
               )
          ) THEN
            RAISE EXCEPTION 'historical model calls conflict with their cost reservations';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM agent.model_calls
            WHERE cost_reservation_id IS NOT NULL
            GROUP BY cost_reservation_id, provider_attempt
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'historical model calls duplicate a reserved provider attempt';
          END IF;
        END $$;
        """
    )
    op.create_unique_constraint(
        "uq_model_call_reserved_attempt",
        "model_calls",
        ["cost_reservation_id", "provider_attempt"],
        schema="agent",
    )
    op.execute(
        """
        CREATE FUNCTION agent.validate_model_call_reservation() RETURNS trigger AS $$
        DECLARE
          reserved_logical_call_id uuid;
          reserved_attempt_limit integer;
          reserved_experiment_id uuid;
          owner_matches boolean;
        BEGIN
          IF NEW.cost_reservation_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT logical_call_id, attempt_limit, experiment_id
          INTO reserved_logical_call_id, reserved_attempt_limit, reserved_experiment_id
          FROM agent.model_call_cost_reservations
          WHERE id = NEW.cost_reservation_id;
          IF NOT FOUND
             OR NEW.logical_call_id <> reserved_logical_call_id
             OR NEW.provider_attempt > reserved_attempt_limit THEN
            RAISE EXCEPTION 'model call conflicts with its cost reservation';
          END IF;
          IF NEW.query_run_id IS NOT NULL THEN
            SELECT EXISTS (
              SELECT 1
              FROM agent.experiment_memberships m
              WHERE m.experiment_id = reserved_experiment_id
                AND m.query_run_id = NEW.query_run_id
            ) INTO owner_matches;
          ELSE
            SELECT EXISTS (
              SELECT 1
              FROM agent.ingestion_attempts a
              JOIN agent.ingestion_jobs j ON j.id = a.job_id
              WHERE a.id = NEW.ingestion_attempt_id
                AND j.experiment_id = reserved_experiment_id
            ) INTO owner_matches;
          END IF;
          IF NOT owner_matches THEN
            RAISE EXCEPTION 'model call owner conflicts with its cost reservation';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER validate_model_call_reservation_trigger
        BEFORE INSERT OR UPDATE OF cost_reservation_id, logical_call_id, provider_attempt,
          query_run_id, ingestion_attempt_id
        ON agent.model_calls
        FOR EACH ROW EXECUTE FUNCTION agent.validate_model_call_reservation();

        CREATE FUNCTION agent.cost_reservation_identity_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
             OR NEW.logical_call_id IS DISTINCT FROM OLD.logical_call_id
             OR NEW.input_token_limit IS DISTINCT FROM OLD.input_token_limit
             OR NEW.output_token_limit IS DISTINCT FROM OLD.output_token_limit
             OR NEW.attempt_limit IS DISTINCT FROM OLD.attempt_limit THEN
            RAISE EXCEPTION 'cost reservation identity and bounds are immutable';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER cost_reservation_identity_immutable_trigger
        BEFORE UPDATE OF experiment_id, logical_call_id, input_token_limit,
          output_token_limit, attempt_limit
        ON agent.model_call_cost_reservations
        FOR EACH ROW EXECUTE FUNCTION agent.cost_reservation_identity_immutable();

        CREATE FUNCTION agent.ingestion_job_experiment_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
             AND EXISTS (
               SELECT 1
               FROM agent.ingestion_attempts a
               WHERE a.job_id = OLD.id
             ) THEN
            RAISE EXCEPTION
              'ingestion job experiment binding is immutable after its first attempt';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER ingestion_job_experiment_immutable_trigger
        BEFORE UPDATE OF experiment_id ON agent.ingestion_jobs
        FOR EACH ROW EXECUTE FUNCTION agent.ingestion_job_experiment_immutable();

        CREATE FUNCTION agent.ingestion_attempt_job_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.job_id IS DISTINCT FROM OLD.job_id THEN
            RAISE EXCEPTION 'ingestion attempt job binding is immutable';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER ingestion_attempt_job_immutable_trigger
        BEFORE UPDATE OF job_id ON agent.ingestion_attempts
        FOR EACH ROW EXECUTE FUNCTION agent.ingestion_attempt_job_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS ingestion_attempt_job_immutable_trigger
          ON agent.ingestion_attempts;
        DROP FUNCTION IF EXISTS agent.ingestion_attempt_job_immutable();
        DROP TRIGGER IF EXISTS ingestion_job_experiment_immutable_trigger
          ON agent.ingestion_jobs;
        DROP FUNCTION IF EXISTS agent.ingestion_job_experiment_immutable();
        DROP TRIGGER IF EXISTS cost_reservation_identity_immutable_trigger
          ON agent.model_call_cost_reservations;
        DROP FUNCTION IF EXISTS agent.cost_reservation_identity_immutable();
        DROP TRIGGER IF EXISTS validate_model_call_reservation_trigger ON agent.model_calls;
        DROP FUNCTION IF EXISTS agent.validate_model_call_reservation();
        """
    )
    op.drop_constraint(
        "uq_model_call_reserved_attempt",
        "model_calls",
        schema="agent",
    )
    op.drop_constraint(
        "ck_cost_reservation_positive_bounds",
        "model_call_cost_reservations",
        schema="agent",
    )
    for name in ("attempt_limit", "output_token_limit", "input_token_limit"):
        op.drop_column("model_call_cost_reservations", name, schema="agent")
