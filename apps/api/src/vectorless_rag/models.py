from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DocumentStatus(StrEnum):
    discovered = "discovered"
    queued = "queued"
    indexing = "indexing"
    ready = "ready"
    unsupported = "unsupported"
    failed = "failed"
    waiting_provider = "waiting_provider"
    deferred = "deferred"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    unsupported = "unsupported"
    waiting_provider = "waiting_provider"
    deferred = "deferred"
    deduplicated = "deduplicated"
    cancelled = "cancelled"
    abandoned = "abandoned"


class IngestionAttemptStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    unsupported = "unsupported"
    abandoned = "abandoned"
    deferred = "deferred"
    cancelled = "cancelled"


class IngestionMode(StrEnum):
    ensure_current = "ensure_current"
    rebuild_index = "rebuild_index"
    reextract_all = "reextract_all"


class IndexLifecycleStatus(StrEnum):
    building = "building"
    verified = "verified"
    active = "active"
    failed = "failed"
    abandoned = "abandoned"
    deduplicated = "deduplicated"


class IndexActivationReason(StrEnum):
    ingestion = "ingestion"
    reuse = "reuse"
    rollback = "rollback"
    repair = "repair"
    migration = "migration"


class ProviderAvailability(StrEnum):
    available = "available"
    unavailable = "unavailable"
    unknown = "unknown"
    unconfigured = "unconfigured"


class ArtifactAction(StrEnum):
    created = "created"
    rebuilt = "rebuilt"
    reused = "reused"
    none = "none"


class ModelCallOutcome(StrEnum):
    started = "started"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    abandoned = "abandoned"


class ModelCallStage(StrEnum):
    ingestion_metadata = "ingestion_metadata"
    pageindex_generation = "pageindex_generation"
    query_route = "query_route"
    query_sql = "query_sql"
    query_document_select = "query_document_select"
    query_node_select = "query_node_select"
    query_sufficiency = "query_sufficiency"
    query_synthesis = "query_synthesis"


class RunFailureCode(StrEnum):
    deadline_exceeded = "deadline_exceeded"
    client_cancelled = "client_cancelled"
    request_failed = "request_failed"
    usage_persistence_failed = "usage_persistence_failed"
    provider_unavailable = "provider_unavailable"
    upstream_invalid_response = "upstream_invalid_response"


class QueryRunStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"


class RunRoute(StrEnum):
    sql = "sql"
    documents = "documents"
    hybrid = "hybrid"
    clarify = "clarify"
    help = "help"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["active_index_version_id", "id"],
            ["agent.index_versions.id", "agent.index_versions.document_id"],
            name="fk_documents_active_index_same_document",
            use_alter=True,
        ),
        Index("ix_documents_arxiv_id", "arxiv_id"),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    arxiv_id: Mapped[str | None] = mapped_column(String(32))
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    pdf_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR)
    submitted_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status", schema="agent"),
        default=DocumentStatus.discovered,
        nullable=False,
    )
    diagnostic: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    active_index_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    index_versions: Mapped[list[IndexVersion]] = relationship(
        back_populates="document", foreign_keys="IndexVersion.document_id"
    )


class IndexVersion(TimestampMixin, Base):
    __tablename__ = "index_versions"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="uq_index_id_document"),
        Index(
            "uq_index_document_recipe_manifest",
            "document_id",
            "recipe_key",
            "manifest_sha256",
            unique=True,
            postgresql_where=text("manifest_sha256 IS NOT NULL"),
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.documents.id"), nullable=False
    )
    version_key: Mapped[str] = mapped_column(String(64), nullable=False)
    pageindex_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_quality: Mapped[float | None] = mapped_column(Float)
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    artifact_schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    recipe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_hash: Mapped[str | None] = mapped_column(String(64))
    pipeline_version: Mapped[str | None] = mapped_column(String(128))
    metadata_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.document_metadata_versions.id")
    )
    manifest_object_key: Mapped[str | None] = mapped_column(String(1024))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    pageindex_description: Mapped[str | None] = mapped_column(Text)
    top_level_outline: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    page_count: Mapped[int | None] = mapped_column(Integer)
    node_count: Mapped[int | None] = mapped_column(Integer)
    max_depth: Mapped[int | None] = mapped_column(Integer)
    quality: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    build_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_attempts.id")
    )
    lifecycle_status: Mapped[IndexLifecycleStatus] = mapped_column(
        Enum(IndexLifecycleStatus, name="index_lifecycle_status", schema="agent"),
        default=IndexLifecycleStatus.verified,
        nullable=False,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document: Mapped[Document] = relationship(
        back_populates="index_versions", foreign_keys=[document_id]
    )


class IngestionJob(TimestampMixin, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "created_at"),
        Index(
            "uq_ingestion_active_document",
            "document_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.documents.id"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", schema="agent"),
        default=JobStatus.queued,
        nullable=False,
    )
    force: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mode: Mapped[IngestionMode] = mapped_column(
        Enum(IngestionMode, name="ingestion_mode", schema="agent"),
        default=IngestionMode.ensure_current,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_stage: Mapped[str | None] = mapped_column(String(80))
    active_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_attempts.id", use_alter=True)
    )
    active_index_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.index_versions.id")
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.experiments.id")
    )
    retry_budget_consumed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_wait_reason: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    diagnostic: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    document: Mapped[Document] = relationship()


class IngestionAttempt(TimestampMixin, Base):
    __tablename__ = "ingestion_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_ingestion_attempt_job_number"),
        CheckConstraint("attempt_number > 0", name="ck_ingestion_attempt_number_positive"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="ck_attempt_page_count"),
        CheckConstraint(
            "artifact_bytes IS NULL OR artifact_bytes >= 0", name="ck_attempt_artifact_bytes"
        ),
        CheckConstraint(
            "elapsed_seconds IS NULL OR elapsed_seconds >= 0", name="ck_attempt_elapsed"
        ),
        Index(
            "uq_ingestion_running_attempt",
            "job_id",
            unique=True,
            postgresql_where=text("status = 'running'::agent.ingestion_attempt_status"),
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_jobs.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[IngestionAttemptStatus] = mapped_column(
        Enum(IngestionAttemptStatus, name="ingestion_attempt_status", schema="agent"),
        default=IngestionAttemptStatus.running,
        nullable=False,
    )
    failure_code: Mapped[str | None] = mapped_column(String(80))
    artifact_action: Mapped[ArtifactAction] = mapped_column(
        Enum(ArtifactAction, name="artifact_action", schema="agent"),
        default=ArtifactAction.none,
        nullable=False,
    )
    page_count: Mapped[int | None] = mapped_column(Integer)
    extraction_quality: Mapped[float | None] = mapped_column(Float)
    artifact_bytes: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    telemetry_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    index_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.index_versions.id", use_alter=True)
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retry_budget_consumed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    job: Mapped[IngestionJob] = relationship(foreign_keys=[job_id])


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = ({"schema": "agent"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentMetadataVersion(Base):
    __tablename__ = "document_metadata_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "recipe_key",
            "content_sha256",
            name="uq_metadata_document_recipe_content",
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.documents.id"), nullable=False
    )
    recipe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_quality: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndexActivation(Base):
    __tablename__ = "index_activations"
    __table_args__ = ({"schema": "agent"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.documents.id"), nullable=False
    )
    prior_index_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.index_versions.id")
    )
    index_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.index_versions.id"), nullable=False
    )
    reason: Mapped[IndexActivationReason] = mapped_column(
        Enum(IndexActivationReason, name="index_activation_reason", schema="agent"),
        nullable=False,
    )
    ingestion_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_attempts.id")
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QueryRun(Base):
    __tablename__ = "query_runs"
    __table_args__ = (Index("ix_runs_thread", "thread_id", "created_at"), {"schema": "agent"})

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.api_keys.id"), nullable=False
    )
    route: Mapped[RunRoute | None] = mapped_column(Enum(RunRoute, name="run_route", schema="agent"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    insufficient: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[RunFailureCode | None] = mapped_column(
        Enum(RunFailureCode, name="run_failure_code", schema="agent")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def query_run_status(run: QueryRun) -> QueryRunStatus:
    if run.failure_code is not None or run.error is not None:
        return QueryRunStatus.failed
    if run.answer is not None:
        return QueryRunStatus.completed
    return QueryRunStatus.running


class ModelCall(TimestampMixin, Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        CheckConstraint(
            "(query_run_id IS NULL) <> (ingestion_attempt_id IS NULL)",
            name="ck_model_call_exactly_one_owner",
        ),
        CheckConstraint(
            "(experiment_id IS NULL) = (cost_reservation_id IS NULL)",
            name="ck_model_call_experiment_reservation_pair",
        ),
        CheckConstraint("provider_attempt > 0", name="ck_model_call_attempt_positive"),
        CheckConstraint(
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
        CheckConstraint(
            """
            NOT usage_available OR
            input_tokens =
              cache_hit_input_tokens + cache_miss_input_tokens + unattributed_input_tokens
            """,
            name="ck_model_call_input_partition",
        ),
        CheckConstraint(
            "NOT usage_available OR cache_write_input_tokens <= cache_miss_input_tokens",
            name="ck_model_call_cache_write_subset",
        ),
        CheckConstraint(
            "NOT usage_available OR reasoning_output_tokens <= output_tokens",
            name="ck_model_call_reasoning_subset",
        ),
        CheckConstraint(
            "NOT usage_available OR total_tokens = input_tokens + output_tokens",
            name="ck_model_call_total_partition",
        ),
        CheckConstraint(
            "NOT cache_attribution_complete OR (usage_available AND unattributed_input_tokens = 0)",
            name="ck_model_call_cache_attribution",
        ),
        Index("ix_model_calls_query_owner", "query_run_id"),
        Index("ix_model_calls_ingestion_owner", "ingestion_attempt_id"),
        Index("ix_model_calls_trace", "trace_id"),
        UniqueConstraint(
            "cost_reservation_id",
            "provider_attempt",
            name="uq_model_call_reserved_attempt",
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.query_runs.id")
    )
    ingestion_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_attempts.id")
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[ModelCallStage] = mapped_column(
        Enum(ModelCallStage, name="model_call_stage", schema="agent"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, nullable=False
    )
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retry_delay_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    prompt_bytes: Mapped[int | None] = mapped_column(BigInteger)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    output_bytes: Mapped[int | None] = mapped_column(BigInteger)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    release: Mapped[str | None] = mapped_column(String(128))
    configuration_hash: Mapped[str | None] = mapped_column(String(64))
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.experiments.id")
    )
    cost_reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.model_call_cost_reservations.id")
    )
    finish_reason: Mapped[str | None] = mapped_column(String(80))
    outcome: Mapped[ModelCallOutcome] = mapped_column(
        Enum(ModelCallOutcome, name="model_call_outcome", schema="agent"),
        default=ModelCallOutcome.started,
        nullable=False,
    )
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_hit_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_miss_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    unattributed_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    reasoning_output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    usage_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cache_attribution_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SqlAudit(Base):
    __tablename__ = "sql_audits"
    __table_args__ = ({"schema": "agent"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.query_runs.id"), nullable=False
    )
    generated_sql: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_sql: Mapped[str | None] = mapped_column(Text)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    explain_cost: Mapped[float | None] = mapped_column(Float)
    row_count: Mapped[int | None] = mapped_column(Integer)
    payload_bytes: Mapped[int | None] = mapped_column(Integer)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RetrievalAudit(Base):
    __tablename__ = "retrieval_audits"
    __table_args__ = ({"schema": "agent"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.query_runs.id"), nullable=False
    )
    candidate_document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    selected_document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    locators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    sufficient: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rounds: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy: Mapped[str] = mapped_column(String(80), default="pageindex", nullable=False)
    strategy_version: Mapped[str | None] = mapped_column(String(128))
    configuration_hash: Mapped[str | None] = mapped_column(String(64))
    index_digests: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, nullable=False)
    ranked_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    node_choices: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    evidence_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overlapping_pages_avoided: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    merged_ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    content_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    progress: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    phase_timings_ms: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RunFeedback(Base):
    __tablename__ = "run_feedback"
    __table_args__ = ({"schema": "agent"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.api_keys.id"), nullable=False
    )
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProviderState(TimestampMixin, Base):
    __tablename__ = "provider_states"
    __table_args__ = ({"schema": "agent"},)

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    availability: Mapped[ProviderAvailability] = mapped_column(
        Enum(ProviderAvailability, name="provider_availability", schema="agent"),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(80))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Experiment(TimestampMixin, Base):
    __tablename__ = "experiments"
    __table_args__ = ({"schema": "agent"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    release_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    index_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    price_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maximum_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invalid_reason: Mapped[str | None] = mapped_column(String(80))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExperimentMembership(Base):
    __tablename__ = "experiment_memberships"
    __table_args__ = (
        Index(
            "uq_experiment_attempt_membership",
            "experiment_id",
            "attempt_id",
            unique=True,
            postgresql_where=text("attempt_id IS NOT NULL"),
        ),
        Index(
            "uq_experiment_query_trial_membership",
            "experiment_id",
            "query_run_id",
            "trial_key",
            unique=True,
            postgresql_where=text("query_run_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(attempt_id IS NULL) <> (query_run_id IS NULL)",
            name="ck_experiment_membership_one_owner",
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.experiments.id"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_attempts.id")
    )
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.query_runs.id")
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    trial_key: Mapped[str] = mapped_column(String(128), nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExperimentIndexVersion(Base):
    __tablename__ = "experiment_index_versions"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "index_version_id",
            name="uq_experiment_index_version",
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.experiments.id"), nullable=False
    )
    index_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.index_versions.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelCallCostReservation(TimestampMixin, Base):
    __tablename__ = "model_call_cost_reservations"
    __table_args__ = ({"schema": "agent"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.experiments.id"), nullable=False
    )
    logical_call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    input_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_token_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    measured_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeasurementClassificationEvent(Base):
    __tablename__ = "measurement_classification_events"
    __table_args__ = (
        CheckConstraint(
            "(ingestion_attempt_id IS NULL) <> (query_run_id IS NULL)",
            name="ck_classification_one_owner",
        ),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ingestion_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_attempts.id")
    )
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.query_runs.id")
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    replacement_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.ingestion_attempts.id")
    )
    prior_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.measurement_classification_events.id")
    )
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
