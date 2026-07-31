"""Serialize reservation state with model-call insertion."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_reservation_state_guard"
down_revision: str | None = "0007_model_call_experiment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_ledger_tables() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          LOOP
            BEGIN
              LOCK TABLE agent.model_call_cost_reservations, agent.model_calls
              IN SHARE ROW EXCLUSIVE MODE NOWAIT;
              EXIT;
            EXCEPTION
              WHEN lock_not_available THEN
                PERFORM pg_sleep(0.05);
            END;
          END LOOP;
        END
        $$;
        """
    )


def _preflight() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM agent.model_calls c
            JOIN agent.model_call_cost_reservations r ON r.id = c.cost_reservation_id
            WHERE c.outcome = 'started'::agent.model_call_outcome
              AND r.status <> 'reserved'
          ) THEN
            RAISE EXCEPTION
              'migration preflight: started model call has terminal cost reservation';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM agent.model_call_cost_reservations r
            JOIN agent.experiments e ON e.id = r.experiment_id
            WHERE r.status = 'reserved'
              AND r.reserved_cost_usd < ceil(
                (
                  r.input_token_limit
                  * greatest(
                    (e.price_snapshot->>'input_per_million_usd')::numeric,
                    (e.price_snapshot->>'cache_hit_input_per_million_usd')::numeric
                  )
                  + r.output_token_limit
                  * (e.price_snapshot->>'output_per_million_usd')::numeric
                )
                * r.attempt_limit
              ) / 1000000
          ) THEN
            RAISE EXCEPTION
              'migration preflight: reserved model call cost reservation is underfunded';
          END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _lock_ledger_tables()
    _preflight()

    op.execute(
        """
        CREATE OR REPLACE FUNCTION agent.validate_model_call_reservation()
        RETURNS trigger AS $$
        DECLARE
          reserved_logical_call_id uuid;
          reserved_attempt_limit integer;
          reserved_experiment_id uuid;
          reserved_status varchar(32);
          owner_matches boolean;
        BEGIN
          IF NEW.cost_reservation_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT logical_call_id, attempt_limit, experiment_id, status
          INTO reserved_logical_call_id, reserved_attempt_limit, reserved_experiment_id,
            reserved_status
          FROM agent.model_call_cost_reservations
          WHERE id = NEW.cost_reservation_id
          FOR UPDATE;
          IF NOT FOUND
             OR reserved_status <> 'reserved'
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

        CREATE FUNCTION agent.validate_cost_reservation_state()
        RETURNS trigger AS $$
        DECLARE
          current_status varchar(32);
          current_reserved_cost numeric(14,6);
          required_reserved_cost numeric;
        BEGIN
          SELECT r.status,
                 r.reserved_cost_usd,
                 ceil(
                   (
                     r.input_token_limit
                     * greatest(
                       (e.price_snapshot->>'input_per_million_usd')::numeric,
                       (e.price_snapshot->>'cache_hit_input_per_million_usd')::numeric
                     )
                     + r.output_token_limit
                     * (e.price_snapshot->>'output_per_million_usd')::numeric
                   )
                   * r.attempt_limit
                 ) / 1000000
          INTO current_status, current_reserved_cost, required_reserved_cost
          FROM agent.model_call_cost_reservations r
          JOIN agent.experiments e ON e.id = r.experiment_id
          WHERE r.id = NEW.id;
          IF NOT FOUND THEN
            RETURN NULL;
          END IF;
          IF current_status <> 'reserved'
             AND EXISTS (
               SELECT 1
               FROM agent.model_calls c
               WHERE c.cost_reservation_id = NEW.id
                 AND c.outcome = 'started'::agent.model_call_outcome
             ) THEN
            RAISE EXCEPTION
              'terminal cost reservation conflicts with a started model call';
          END IF;
          IF current_status = 'reserved'
             AND current_reserved_cost < required_reserved_cost THEN
            RAISE EXCEPTION 'reserved model call cost reservation is underfunded';
          END IF;
          RETURN NULL;
        END
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER validate_cost_reservation_state_trigger
        AFTER INSERT OR UPDATE ON agent.model_call_cost_reservations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION agent.validate_cost_reservation_state();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER validate_cost_reservation_state_trigger
          ON agent.model_call_cost_reservations;
        DROP FUNCTION agent.validate_cost_reservation_state();

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
        """
    )
