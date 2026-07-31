"""Start reserved model calls atomically."""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_atomic_model_call_start"
down_revision: str | None = "0008_reservation_state_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION agent.reserve_model_call(
          p_query_run_id uuid,
          p_ingestion_attempt_id uuid,
          p_reservation_id uuid,
          p_logical_call_id uuid,
          p_input_token_limit bigint,
          p_output_token_limit integer,
          p_attempt_limit integer,
          p_legacy_max_input_tokens bigint,
          p_legacy_max_output_tokens integer,
          p_legacy_query_max_attempts integer
        )
        RETURNS TABLE (
          reservation_id uuid,
          input_token_limit bigint,
          output_token_limit integer,
          attempt_limit integer
        )
        LANGUAGE sql
        SET search_path = pg_catalog, agent
        AS $$
          WITH owner_experiment AS (
              SELECT j.experiment_id, 'ingestion' AS owner_kind
              FROM agent.ingestion_attempts a
              JOIN agent.ingestion_jobs j ON j.id = a.job_id
              WHERE a.id = p_ingestion_attempt_id
                AND j.experiment_id IS NOT NULL
              UNION ALL
              SELECT m.experiment_id, 'query' AS owner_kind
              FROM agent.experiment_memberships m
              WHERE m.query_run_id = p_query_run_id
              ORDER BY 1
              LIMIT 1
          ),
          experiment AS (
              SELECT e.*, o.owner_kind
              FROM agent.experiments e
              JOIN owner_experiment o ON o.experiment_id = e.id
              WHERE e.valid
          ),
          inserted AS (
              INSERT INTO agent.model_call_cost_reservations (
                  id, experiment_id, logical_call_id, reserved_cost_usd,
                  input_token_limit, output_token_limit, attempt_limit, status
              )
              SELECT
                  p_reservation_id,
                  e.id,
                  p_logical_call_id,
                  ceil(
                      (
                          p_input_token_limit
                          * greatest(
                              (e.price_snapshot->>'input_per_million_usd')::numeric,
                              (
                                e.price_snapshot
                                ->>'cache_hit_input_per_million_usd'
                              )::numeric
                          )
                          + p_output_token_limit
                          * (e.price_snapshot->>'output_per_million_usd')::numeric
                      )
                      * p_attempt_limit
                  ) / 1000000,
                  p_input_token_limit,
                  p_output_token_limit,
                  p_attempt_limit,
                  'reserved'
              FROM experiment e
              WHERE p_input_token_limit > 0
                AND p_output_token_limit > 0
                AND p_attempt_limit > 0
                AND p_input_token_limit <= coalesce(
                  (e.price_snapshot->>'max_input_tokens')::numeric,
                  p_legacy_max_input_tokens
                )
                AND p_output_token_limit <= coalesce(
                  (e.price_snapshot->>'max_output_tokens')::numeric,
                  p_legacy_max_output_tokens
                )
                AND p_attempt_limit <= coalesce(
                  (e.price_snapshot->>'max_attempts')::numeric,
                  CASE
                    WHEN e.owner_kind = 'query' THEN p_legacy_query_max_attempts
                  END
                )
                AND NOT EXISTS (
                  SELECT 1
                  FROM agent.model_call_cost_reservations r
                  WHERE r.experiment_id = e.id
                    AND r.logical_call_id = p_logical_call_id
                )
              RETURNING id, input_token_limit, output_token_limit, attempt_limit
          ),
          existing AS (
              SELECT r.id, r.input_token_limit, r.output_token_limit, r.attempt_limit
              FROM agent.model_call_cost_reservations r
              JOIN experiment e ON e.id = r.experiment_id
              WHERE r.logical_call_id = p_logical_call_id
              FOR UPDATE OF r
          ),
          resolved AS (
              SELECT id, input_token_limit, output_token_limit, attempt_limit
              FROM inserted
              UNION ALL
              SELECT id, input_token_limit, output_token_limit, attempt_limit
              FROM existing
          )
          SELECT id, input_token_limit, output_token_limit, attempt_limit
          FROM resolved
          UNION ALL
          SELECT NULL::uuid, NULL::bigint, NULL::integer, NULL::integer
          FROM owner_experiment
          WHERE NOT EXISTS (SELECT 1 FROM resolved)
          LIMIT 1
        $$;

        CREATE FUNCTION agent.start_model_call(
          p_id uuid,
          p_query_run_id uuid,
          p_ingestion_attempt_id uuid,
          p_trace_id text,
          p_stage agent.model_call_stage,
          p_model text,
          p_provider_attempt integer,
          p_logical_call_id uuid,
          p_operation text,
          p_retry_delay_ms integer,
          p_prompt_sha256 text,
          p_prompt_bytes bigint,
          p_release text,
          p_configuration_hash text,
          p_reservation_id uuid,
          p_input_token_limit bigint,
          p_output_token_limit integer,
          p_attempt_limit integer,
          p_legacy_max_input_tokens bigint,
          p_legacy_max_output_tokens integer,
          p_legacy_query_max_attempts integer,
          p_require_cost_reservation boolean
        )
        RETURNS TABLE (
          call_id uuid,
          cost_reservation_id uuid,
          reservation_input_token_limit bigint,
          reservation_output_token_limit integer,
          reservation_attempt_limit integer
        )
        LANGUAGE plpgsql
        SET search_path = pg_catalog, agent
        AS $$
        DECLARE
          v_reservation_id uuid;
          v_input_token_limit bigint;
          v_output_token_limit integer;
          v_attempt_limit integer;
          v_reservation_resolved boolean;
        BEGIN
          SELECT reservation_id, input_token_limit, output_token_limit, attempt_limit
          INTO
            v_reservation_id,
            v_input_token_limit,
            v_output_token_limit,
            v_attempt_limit
          FROM agent.reserve_model_call(
            p_query_run_id,
            p_ingestion_attempt_id,
            p_reservation_id,
            p_logical_call_id,
            p_input_token_limit,
            p_output_token_limit,
            p_attempt_limit,
            p_legacy_max_input_tokens,
            p_legacy_max_output_tokens,
            p_legacy_query_max_attempts
          )
          LIMIT 1;
          v_reservation_resolved := FOUND;

          IF (v_reservation_resolved AND v_reservation_id IS NULL)
             OR (NOT v_reservation_resolved AND p_require_cost_reservation) THEN
            RETURN QUERY
            SELECT
              NULL::uuid,
              NULL::uuid,
              NULL::bigint,
              NULL::integer,
              NULL::integer;
            RETURN;
          END IF;

          RETURN QUERY
          WITH inserted_call AS (
              INSERT INTO agent.model_calls AS c (
                  id, query_run_id, ingestion_attempt_id, trace_id, stage, model,
                  provider_attempt, logical_call_id, operation, retry_delay_ms,
                  prompt_sha256, prompt_bytes, release, configuration_hash,
                  experiment_id, cost_reservation_id, outcome, usage_available,
                  cache_attribution_complete
              )
              SELECT
                  p_id,
                  p_query_run_id,
                  p_ingestion_attempt_id,
                  p_trace_id,
                  p_stage,
                  p_model,
                  p_provider_attempt,
                  p_logical_call_id,
                  p_operation,
                  p_retry_delay_ms,
                  p_prompt_sha256,
                  p_prompt_bytes,
                  p_release,
                  p_configuration_hash,
                  (
                    SELECT r.experiment_id
                    FROM agent.model_call_cost_reservations r
                    WHERE r.id = v_reservation_id
                  ),
                  v_reservation_id,
                  'started'::agent.model_call_outcome,
                  false,
                  false
              WHERE
                  (
                    p_query_run_id IS NOT NULL
                    AND EXISTS (
                      SELECT 1 FROM agent.query_runs q WHERE q.id = p_query_run_id
                    )
                  )
                  OR
                  (
                    p_ingestion_attempt_id IS NOT NULL
                    AND EXISTS (
                      SELECT 1
                      FROM agent.ingestion_attempts a
                      WHERE a.id = p_ingestion_attempt_id
                        AND a.status = 'running'::agent.ingestion_attempt_status
                    )
                  )
              RETURNING c.id, c.cost_reservation_id
          )
          SELECT
            inserted_call.id,
            inserted_call.cost_reservation_id,
            v_input_token_limit,
            v_output_token_limit,
            v_attempt_limit
          FROM inserted_call;
        END
        $$;

        REVOKE ALL ON FUNCTION agent.start_model_call(
          uuid, uuid, uuid, text, agent.model_call_stage, text, integer, uuid,
          text, integer, text, bigint, text, text, uuid, bigint, integer,
          integer, bigint, integer, integer, boolean
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION agent.reserve_model_call(
          uuid, uuid, uuid, uuid, bigint, integer, integer, bigint, integer, integer
        ) FROM PUBLIC;
        DO $grant$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
            GRANT EXECUTE ON FUNCTION agent.start_model_call(
              uuid, uuid, uuid, text, agent.model_call_stage, text, integer, uuid,
              text, integer, text, bigint, text, text, uuid, bigint, integer,
              integer, bigint, integer, integer, boolean
            ) TO rag_app;
            GRANT EXECUTE ON FUNCTION agent.reserve_model_call(
              uuid, uuid, uuid, uuid, bigint, integer, integer, bigint, integer, integer
            ) TO rag_app;
          END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION agent.start_model_call(
          uuid, uuid, uuid, text, agent.model_call_stage, text, integer, uuid,
          text, integer, text, bigint, text, text, uuid, bigint, integer,
          integer, bigint, integer, integer, boolean
        );
        DROP FUNCTION agent.reserve_model_call(
          uuid, uuid, uuid, uuid, bigint, integer, integer, bigint, integer, integer
        );
        """
    )
