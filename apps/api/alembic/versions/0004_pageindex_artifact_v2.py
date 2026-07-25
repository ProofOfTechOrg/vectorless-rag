"""Add immutable PageIndex v2 artifacts, activation history, and leased ingestion."""

# pyright: reportUnknownArgumentType=false

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_pageindex_artifact_v2"
down_revision: str | None = "0003_usage_accounting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _preflight() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM agent.documents d
            JOIN agent.index_versions i ON i.id = d.active_index_version_id
            WHERE i.document_id <> d.id
          ) THEN
            RAISE EXCEPTION 'migration preflight: active index belongs to another document';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM agent.ingestion_jobs
            WHERE status IN (
              'queued'::agent.job_status,
              'running'::agent.job_status
            )
            GROUP BY document_id
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'migration preflight: duplicate active ingestion jobs';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM agent.ingestion_attempts
            WHERE status = 'running'::agent.ingestion_attempt_status
            GROUP BY job_id
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'migration preflight: duplicate running ingestion attempts';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM agent.documents
            WHERE (status = 'ready'::agent.document_status)
              <> (active_index_version_id IS NOT NULL)
          ) THEN
            RAISE EXCEPTION 'migration preflight: document readiness and pointer disagree';
          END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _preflight()

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE agent.document_status ADD VALUE IF NOT EXISTS 'waiting_provider'")
        op.execute("ALTER TYPE agent.document_status ADD VALUE IF NOT EXISTS 'deferred'")
        op.execute("ALTER TYPE agent.job_status ADD VALUE IF NOT EXISTS 'waiting_provider'")
        op.execute("ALTER TYPE agent.job_status ADD VALUE IF NOT EXISTS 'deferred'")
        op.execute("ALTER TYPE agent.job_status ADD VALUE IF NOT EXISTS 'deduplicated'")
        op.execute("ALTER TYPE agent.job_status ADD VALUE IF NOT EXISTS 'cancelled'")
        op.execute("ALTER TYPE agent.job_status ADD VALUE IF NOT EXISTS 'abandoned'")
        op.execute("ALTER TYPE agent.ingestion_attempt_status ADD VALUE IF NOT EXISTS 'deferred'")
        op.execute("ALTER TYPE agent.ingestion_attempt_status ADD VALUE IF NOT EXISTS 'cancelled'")
    op.execute(
        "CREATE TYPE agent.ingestion_mode AS ENUM "
        "('ensure_current','rebuild_index','reextract_all')"
    )
    op.execute(
        "CREATE TYPE agent.index_lifecycle_status AS ENUM "
        "('building','verified','active','failed','abandoned','deduplicated')"
    )
    op.execute(
        "CREATE TYPE agent.index_activation_reason AS ENUM "
        "('ingestion','reuse','rollback','repair','migration')"
    )

    op.create_table(
        "document_metadata_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.documents.id"),
            nullable=False,
        ),
        sa.Column("recipe_key", sa.String(64), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("metadata", postgresql.JSONB, nullable=False),
        sa.Column("page_count", sa.Integer, nullable=False),
        sa.Column("extraction_quality", sa.Float, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "document_id",
            "recipe_key",
            "content_sha256",
            name="uq_metadata_document_recipe_content",
        ),
        sa.CheckConstraint("page_count > 0", name="ck_metadata_page_count"),
        sa.CheckConstraint(
            "extraction_quality >= 0 AND extraction_quality <= 1",
            name="ck_metadata_extraction_quality",
        ),
        schema="agent",
    )

    op.drop_constraint(
        "uq_index_document_version", "index_versions", schema="agent", type_="unique"
    )
    op.add_column(
        "index_versions",
        sa.Column("artifact_schema_version", sa.Integer, server_default="1", nullable=False),
        schema="agent",
    )
    op.add_column("index_versions", sa.Column("recipe_key", sa.String(64)), schema="agent")
    op.add_column("index_versions", sa.Column("configuration_hash", sa.String(64)), schema="agent")
    op.add_column("index_versions", sa.Column("pipeline_version", sa.String(128)), schema="agent")
    op.add_column(
        "index_versions",
        sa.Column(
            "metadata_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.document_metadata_versions.id"),
        ),
        schema="agent",
    )
    op.add_column(
        "index_versions", sa.Column("manifest_object_key", sa.String(1024)), schema="agent"
    )
    op.add_column("index_versions", sa.Column("manifest_sha256", sa.String(64)), schema="agent")
    op.add_column("index_versions", sa.Column("pageindex_description", sa.Text), schema="agent")
    op.add_column(
        "index_versions",
        sa.Column(
            "top_level_outline",
            postgresql.JSONB,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        schema="agent",
    )
    op.add_column("index_versions", sa.Column("page_count", sa.Integer), schema="agent")
    op.add_column("index_versions", sa.Column("node_count", sa.Integer), schema="agent")
    op.add_column("index_versions", sa.Column("max_depth", sa.Integer), schema="agent")
    op.add_column(
        "index_versions",
        sa.Column(
            "quality",
            postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="agent",
    )
    op.add_column(
        "index_versions",
        sa.Column(
            "build_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_attempts.id"),
        ),
        schema="agent",
    )
    op.add_column(
        "index_versions",
        sa.Column(
            "lifecycle_status",
            postgresql.ENUM(name="index_lifecycle_status", schema="agent", create_type=False),
        ),
        schema="agent",
    )
    op.add_column(
        "index_versions", sa.Column("verified_at", sa.DateTime(timezone=True)), schema="agent"
    )
    op.execute(
        """
        UPDATE agent.index_versions i
        SET recipe_key = i.version_key,
            lifecycle_status = CASE
              WHEN EXISTS (
                SELECT 1 FROM agent.documents d
                WHERE d.active_index_version_id = i.id
              ) THEN 'active'::agent.index_lifecycle_status
              ELSE 'verified'::agent.index_lifecycle_status
            END,
            verified_at = coalesce(i.updated_at, i.created_at),
            pipeline_version = i.pageindex_version
        """
    )
    op.alter_column("index_versions", "recipe_key", nullable=False, schema="agent")
    op.alter_column("index_versions", "lifecycle_status", nullable=False, schema="agent")
    op.create_unique_constraint(
        "uq_index_id_document", "index_versions", ["id", "document_id"], schema="agent"
    )
    op.create_index(
        "uq_index_document_recipe_manifest",
        "index_versions",
        ["document_id", "recipe_key", "manifest_sha256"],
        unique=True,
        schema="agent",
        postgresql_where=sa.text("manifest_sha256 IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_index_artifact_schema",
        "index_versions",
        "artifact_schema_version IN (1, 2)",
        schema="agent",
    )
    op.create_check_constraint(
        "ck_index_counts",
        "index_versions",
        "(page_count IS NULL OR page_count > 0) AND "
        "(node_count IS NULL OR (node_count > 0 AND node_count <= 20000)) AND "
        "(max_depth IS NULL OR (max_depth >= 0 AND max_depth <= 64))",
        schema="agent",
    )

    op.drop_constraint("fk_documents_active_index", "documents", schema="agent", type_="foreignkey")
    op.create_foreign_key(
        "fk_documents_active_index_same_document",
        "documents",
        "index_versions",
        ["active_index_version_id", "id"],
        ["id", "document_id"],
        source_schema="agent",
        referent_schema="agent",
    )

    op.add_column(
        "ingestion_jobs",
        sa.Column(
            "mode",
            postgresql.ENUM(name="ingestion_mode", schema="agent", create_type=False),
            server_default="ensure_current",
            nullable=False,
        ),
        schema="agent",
    )
    op.add_column(
        "ingestion_jobs", sa.Column("lease_token", postgresql.UUID(as_uuid=True)), schema="agent"
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        schema="agent",
    )
    op.add_column(
        "ingestion_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)), schema="agent"
    )
    op.add_column("ingestion_jobs", sa.Column("current_stage", sa.String(80)), schema="agent")
    op.add_column(
        "ingestion_jobs",
        sa.Column(
            "active_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_attempts.id", use_alter=True),
        ),
        schema="agent",
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column(
            "active_index_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.index_versions.id"),
        ),
        schema="agent",
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("retry_budget_consumed", sa.Integer, server_default="0", nullable=False),
        schema="agent",
    )
    op.add_column(
        "ingestion_jobs", sa.Column("provider_wait_reason", sa.String(80)), schema="agent"
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        schema="agent",
    )
    op.execute(
        """
        UPDATE agent.ingestion_jobs
        SET mode = CASE WHEN force
          THEN 'rebuild_index'::agent.ingestion_mode
          ELSE 'ensure_current'::agent.ingestion_mode
        END,
        retry_budget_consumed = attempts,
        is_active = status IN (
          'queued'::agent.job_status,
          'running'::agent.job_status
        )
        """
    )
    op.create_index(
        "uq_ingestion_active_document",
        "ingestion_jobs",
        ["document_id"],
        unique=True,
        schema="agent",
        postgresql_where=sa.text("is_active"),
    )
    op.create_check_constraint(
        "ck_ingestion_retry_budget",
        "ingestion_jobs",
        "retry_budget_consumed >= 0",
        schema="agent",
    )

    op.add_column(
        "ingestion_attempts",
        sa.Column(
            "index_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.index_versions.id", use_alter=True),
        ),
        schema="agent",
    )
    op.add_column(
        "ingestion_attempts",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        schema="agent",
    )
    op.add_column(
        "ingestion_attempts",
        sa.Column("retry_budget_consumed", sa.Boolean, server_default=sa.true(), nullable=False),
        schema="agent",
    )
    op.create_index(
        "uq_ingestion_running_attempt",
        "ingestion_attempts",
        ["job_id"],
        unique=True,
        schema="agent",
        postgresql_where=sa.text("status = 'running'::agent.ingestion_attempt_status"),
    )

    op.create_table(
        "index_activations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.documents.id"),
            nullable=False,
        ),
        sa.Column(
            "prior_index_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.index_versions.id"),
        ),
        sa.Column(
            "index_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.index_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "reason",
            postgresql.ENUM(name="index_activation_reason", schema="agent", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "ingestion_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.ingestion_attempts.id"),
        ),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="agent",
    )

    op.execute(
        """
        CREATE FUNCTION agent.reject_immutable_row() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'immutable row in %.%', TG_TABLE_SCHEMA, TG_TABLE_NAME;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER document_metadata_versions_immutable
        BEFORE UPDATE OR DELETE ON agent.document_metadata_versions
        FOR EACH ROW EXECUTE FUNCTION agent.reject_immutable_row();

        CREATE FUNCTION agent.index_version_content_immutable() RETURNS trigger AS $$
        BEGIN
          IF OLD.lifecycle_status IN (
            'verified'::agent.index_lifecycle_status,
            'active'::agent.index_lifecycle_status,
            'deduplicated'::agent.index_lifecycle_status
          ) AND (
            NEW.document_id IS DISTINCT FROM OLD.document_id OR
            NEW.version_key IS DISTINCT FROM OLD.version_key OR
            NEW.artifact_schema_version IS DISTINCT FROM OLD.artifact_schema_version OR
            NEW.recipe_key IS DISTINCT FROM OLD.recipe_key OR
            NEW.configuration_hash IS DISTINCT FROM OLD.configuration_hash OR
            NEW.pipeline_version IS DISTINCT FROM OLD.pipeline_version OR
            NEW.metadata_version_id IS DISTINCT FROM OLD.metadata_version_id OR
            NEW.artifact_object_key IS DISTINCT FROM OLD.artifact_object_key OR
            NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256 OR
            NEW.manifest_object_key IS DISTINCT FROM OLD.manifest_object_key OR
            NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256 OR
            NEW.pageindex_description IS DISTINCT FROM OLD.pageindex_description OR
            NEW.top_level_outline IS DISTINCT FROM OLD.top_level_outline OR
            NEW.page_count IS DISTINCT FROM OLD.page_count OR
            NEW.node_count IS DISTINCT FROM OLD.node_count OR
            NEW.max_depth IS DISTINCT FROM OLD.max_depth OR
            NEW.quality IS DISTINCT FROM OLD.quality
          ) THEN
            RAISE EXCEPTION 'verified index content is immutable';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER index_version_content_immutable_trigger
        BEFORE UPDATE ON agent.index_versions
        FOR EACH ROW EXECUTE FUNCTION agent.index_version_content_immutable();

        CREATE FUNCTION agent.validate_active_index_pointer() RETURNS trigger AS $$
        DECLARE
          candidate agent.index_versions%ROWTYPE;
        BEGIN
          IF NEW.active_index_version_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT * INTO candidate
          FROM agent.index_versions
          WHERE id = NEW.active_index_version_id;
          IF NOT FOUND OR candidate.document_id <> NEW.id THEN
            RAISE EXCEPTION 'active index must belong to the document';
          END IF;
          IF candidate.lifecycle_status NOT IN (
            'verified'::agent.index_lifecycle_status,
            'active'::agent.index_lifecycle_status
          ) OR candidate.verified_at IS NULL THEN
            RAISE EXCEPTION 'active index must be verified';
          END IF;
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER validate_active_index_pointer_trigger
        BEFORE INSERT OR UPDATE OF active_index_version_id ON agent.documents
        FOR EACH ROW EXECUTE FUNCTION agent.validate_active_index_pointer();

        CREATE FUNCTION agent.index_activations_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'index activations are append only';
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER index_activations_append_only_trigger
        BEFORE UPDATE OR DELETE ON agent.index_activations
        FOR EACH ROW EXECUTE FUNCTION agent.index_activations_append_only();
        """
    )
    op.execute(
        """
        INSERT INTO agent.index_activations (
          id, document_id, prior_index_version_id, index_version_id, reason, actor
        )
        SELECT gen_random_uuid(), id, NULL, active_index_version_id,
               'migration'::agent.index_activation_reason, 'migration-0004'
        FROM agent.documents
        WHERE active_index_version_id IS NOT NULL
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
            GRANT SELECT, INSERT ON agent.document_metadata_versions,
              agent.index_activations TO rag_app;
            REVOKE UPDATE, DELETE ON agent.document_metadata_versions,
              agent.index_activations FROM rag_app;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_reader') THEN
            REVOKE ALL ON agent.document_metadata_versions,
              agent.index_activations FROM rag_reader;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS index_activations_append_only_trigger
          ON agent.index_activations;
        DROP FUNCTION IF EXISTS agent.index_activations_append_only();
        DROP TRIGGER IF EXISTS validate_active_index_pointer_trigger ON agent.documents;
        DROP FUNCTION IF EXISTS agent.validate_active_index_pointer();
        DROP TRIGGER IF EXISTS index_version_content_immutable_trigger
          ON agent.index_versions;
        DROP FUNCTION IF EXISTS agent.index_version_content_immutable();
        DROP TRIGGER IF EXISTS document_metadata_versions_immutable
          ON agent.document_metadata_versions;
        DROP FUNCTION IF EXISTS agent.reject_immutable_row();
        """
    )
    op.drop_table("index_activations", schema="agent")
    op.drop_index("uq_ingestion_running_attempt", table_name="ingestion_attempts", schema="agent")
    for column in ("retry_budget_consumed", "lease_token", "index_version_id"):
        op.drop_column("ingestion_attempts", column, schema="agent")
    op.drop_index("uq_ingestion_active_document", table_name="ingestion_jobs", schema="agent")
    for column in (
        "is_active",
        "provider_wait_reason",
        "retry_budget_consumed",
        "active_index_version_id",
        "active_attempt_id",
        "current_stage",
        "heartbeat_at",
        "lease_expires_at",
        "lease_token",
        "mode",
    ):
        op.drop_column("ingestion_jobs", column, schema="agent")
    op.drop_constraint(
        "fk_documents_active_index_same_document",
        "documents",
        schema="agent",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_documents_active_index",
        "documents",
        "index_versions",
        ["active_index_version_id"],
        ["id"],
        source_schema="agent",
        referent_schema="agent",
    )
    op.drop_constraint("ck_index_counts", "index_versions", schema="agent", type_="check")
    op.drop_constraint("ck_index_artifact_schema", "index_versions", schema="agent", type_="check")
    op.drop_index("uq_index_document_recipe_manifest", table_name="index_versions", schema="agent")
    op.drop_constraint("uq_index_id_document", "index_versions", schema="agent", type_="unique")
    for column in (
        "verified_at",
        "lifecycle_status",
        "build_attempt_id",
        "quality",
        "max_depth",
        "node_count",
        "page_count",
        "top_level_outline",
        "pageindex_description",
        "manifest_sha256",
        "manifest_object_key",
        "metadata_version_id",
        "pipeline_version",
        "configuration_hash",
        "recipe_key",
        "artifact_schema_version",
    ):
        op.drop_column("index_versions", column, schema="agent")
    op.create_unique_constraint(
        "uq_index_document_version",
        "index_versions",
        ["document_id", "version_key"],
        schema="agent",
    )
    op.drop_table("document_metadata_versions", schema="agent")
    op.execute("DROP TYPE agent.index_activation_reason")
    op.execute("DROP TYPE agent.index_lifecycle_status")
    op.execute("DROP TYPE agent.ingestion_mode")
