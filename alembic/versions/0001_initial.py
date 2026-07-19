"""Initial application schema and curated SQL views."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

document_status = postgresql.ENUM(
    "discovered",
    "queued",
    "indexing",
    "ready",
    "unsupported",
    "failed",
    name="document_status",
    schema="agent",
    create_type=False,
)
job_status = postgresql.ENUM(
    "queued",
    "running",
    "completed",
    "failed",
    "unsupported",
    name="job_status",
    schema="agent",
    create_type=False,
)
run_route = postgresql.ENUM(
    "sql", "documents", "hybrid", "clarify", name="run_route", schema="agent", create_type=False
)


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agent")
    op.execute(
        "CREATE TYPE agent.document_status AS ENUM "
        "('discovered','queued','indexing','ready','unsupported','failed')"
    )
    op.execute(
        "CREATE TYPE agent.job_status AS ENUM "
        "('queued','running','completed','failed','unsupported')"
    )
    op.execute("CREATE TYPE agent.run_route AS ENUM ('sql','documents','hybrid','clarify')")
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sha256", sa.String(64), unique=True, nullable=False),
        sa.Column("arxiv_id", sa.String(32)),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("pdf_object_key", sa.String(1024), nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("authors", postgresql.ARRAY(sa.Text), server_default="{}", nullable=False),
        sa.Column("abstract", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("topics", postgresql.ARRAY(sa.Text), server_default="{}", nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR),
        sa.Column("submitted_date", sa.Date),
        sa.Column("status", document_status, server_default="discovered", nullable=False),
        sa.Column("diagnostic", sa.Text),
        sa.Column("page_count", sa.Integer),
        sa.Column("active_index_version_id", postgresql.UUID(as_uuid=True)),
        *timestamps(),
        schema="agent",
    )
    op.create_index("ix_documents_arxiv_id", "documents", ["arxiv_id"], schema="agent")
    op.execute("""CREATE FUNCTION agent.documents_search_vector_update() RETURNS trigger AS $$
      BEGIN
        NEW.search_vector := to_tsvector(
          'english', coalesce(NEW.title,'') || ' ' || coalesce(NEW.abstract,'') || ' ' ||
          coalesce(NEW.description,'') || ' ' || array_to_string(NEW.topics, ' ')
        );
        RETURN NEW;
      END
      $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER documents_search_vector_trigger
      BEFORE INSERT OR UPDATE OF title, abstract, description, topics ON agent.documents
      FOR EACH ROW EXECUTE FUNCTION agent.documents_search_vector_update()""")
    op.execute("CREATE INDEX ix_documents_search ON agent.documents USING gin (search_vector)")
    op.create_table(
        "index_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.documents.id"),
            nullable=False,
        ),
        sa.Column("version_key", sa.String(64), nullable=False),
        sa.Column("pageindex_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("artifact_object_key", sa.String(1024), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("extraction_quality", sa.Float),
        sa.Column("elapsed_seconds", sa.Float),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        *timestamps(),
        sa.UniqueConstraint("document_id", "version_key", name="uq_index_document_version"),
        schema="agent",
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
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.documents.id"),
            nullable=False,
        ),
        sa.Column("status", job_status, server_default="queued", nullable=False),
        sa.Column("force", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("attempts", sa.Integer, server_default="0", nullable=False),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("diagnostic", sa.Text),
        sa.Column("metrics", postgresql.JSONB, server_default="{}", nullable=False),
        *timestamps(),
        schema="agent",
    )
    op.create_index("ix_jobs_claim", "ingestion_jobs", ["status", "created_at"], schema="agent")
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("prefix", sa.String(16), unique=True, nullable=False),
        sa.Column("key_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("disabled", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        *timestamps(),
        schema="agent",
    )
    op.create_table(
        "query_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id", sa.String(64), unique=True, nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.api_keys.id"),
            nullable=False,
        ),
        sa.Column("route", run_route),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text),
        sa.Column("citations", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("insufficient", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("latency_ms", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="agent",
    )
    op.create_index("ix_runs_thread", "query_runs", ["thread_id", "created_at"], schema="agent")
    op.create_table(
        "sql_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.query_runs.id"),
            nullable=False,
        ),
        sa.Column("generated_sql", sa.Text, nullable=False),
        sa.Column("normalized_sql", sa.Text),
        sa.Column("allowed", sa.Boolean, nullable=False),
        sa.Column("rejection_reason", sa.Text),
        sa.Column("explain_cost", sa.Float),
        sa.Column("row_count", sa.Integer),
        sa.Column("payload_bytes", sa.Integer),
        sa.Column("result_hash", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="agent",
    )
    op.create_table(
        "retrieval_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.query_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_document_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column(
            "selected_document_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False
        ),
        sa.Column("locators", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("sufficient", sa.Boolean, nullable=False),
        sa.Column("rounds", sa.Integer, nullable=False),
        sa.Column("evidence_bytes", sa.BigInteger, nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="agent",
    )
    op.create_table(
        "run_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.api_keys.id"),
            nullable=False,
        ),
        sa.Column("value", sa.Integer, nullable=False),
        sa.Column("comment", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("value IN (-1, 1)", name="ck_feedback_value"),
        schema="agent",
    )
    op.execute("""CREATE VIEW agent.paper_catalog_v1 WITH (security_barrier=true) AS
      SELECT id AS document_id, arxiv_id, title, authors, abstract, topics, submitted_date,
             status::text AS status, page_count, created_at, updated_at
      FROM agent.documents""")
    op.execute("""CREATE VIEW agent.ingestion_summary_v1 WITH (security_barrier=true) AS
      SELECT status::text AS status, count(*)::bigint AS document_count,
             min(created_at) AS earliest_created_at, max(updated_at) AS latest_updated_at
      FROM agent.documents GROUP BY status""")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
        GRANT USAGE ON SCHEMA agent TO rag_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent TO rag_app;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_reader') THEN
        GRANT USAGE ON SCHEMA agent TO rag_reader;
        GRANT SELECT ON agent.paper_catalog_v1, agent.ingestion_summary_v1 TO rag_reader;
      END IF;
    END $$""")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS agent CASCADE")
