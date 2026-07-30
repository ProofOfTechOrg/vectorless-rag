"""Make reserved model-call experiment ownership explicit and consistent."""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_model_call_experiment"
down_revision: str | None = "0006_bound_cost_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent.model_calls c
        SET experiment_id = r.experiment_id
        FROM agent.model_call_cost_reservations r
        WHERE r.id = c.cost_reservation_id
          AND c.experiment_id IS DISTINCT FROM r.experiment_id
        """
    )
    op.create_check_constraint(
        "ck_model_call_experiment_reservation_pair",
        "model_calls",
        "(experiment_id IS NULL) = (cost_reservation_id IS NULL)",
        schema="agent",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION agent.validate_model_call_reservation()
        RETURNS trigger AS $$
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
             OR NEW.provider_attempt > reserved_attempt_limit
             OR NEW.experiment_id IS DISTINCT FROM reserved_experiment_id THEN
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

        DROP TRIGGER validate_model_call_reservation_trigger ON agent.model_calls;
        CREATE TRIGGER validate_model_call_reservation_trigger
        BEFORE INSERT OR UPDATE OF cost_reservation_id, experiment_id, logical_call_id,
          provider_attempt, query_run_id, ingestion_attempt_id
        ON agent.model_calls
        FOR EACH ROW EXECUTE FUNCTION agent.validate_model_call_reservation();

        CREATE FUNCTION agent.model_call_identity_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.query_run_id IS DISTINCT FROM OLD.query_run_id
             OR NEW.ingestion_attempt_id IS DISTINCT FROM OLD.ingestion_attempt_id
             OR NEW.logical_call_id IS DISTINCT FROM OLD.logical_call_id
             OR NEW.provider_attempt IS DISTINCT FROM OLD.provider_attempt
             OR NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
             OR NEW.cost_reservation_id IS DISTINCT FROM OLD.cost_reservation_id THEN
            RAISE EXCEPTION 'model call identity is immutable';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER model_call_identity_immutable_trigger
        BEFORE UPDATE OF query_run_id, ingestion_attempt_id, logical_call_id,
          provider_attempt, experiment_id, cost_reservation_id
        ON agent.model_calls
        FOR EACH ROW EXECUTE FUNCTION agent.model_call_identity_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER model_call_identity_immutable_trigger ON agent.model_calls;
        DROP FUNCTION agent.model_call_identity_immutable();
        DROP TRIGGER validate_model_call_reservation_trigger ON agent.model_calls;

        CREATE OR REPLACE FUNCTION agent.validate_model_call_reservation()
        RETURNS trigger AS $$
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
        """
    )
    op.drop_constraint(
        "ck_model_call_experiment_reservation_pair",
        "model_calls",
        schema="agent",
    )
