from __future__ import annotations

import uuid
from datetime import date, datetime
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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    unsupported = "unsupported"


class IngestionAttemptStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    unsupported = "unsupported"
    abandoned = "abandoned"


class ArtifactAction(StrEnum):
    created = "created"
    rebuilt = "rebuilt"
    reused = "reused"
    none = "none"


class ModelCallOutcome(StrEnum):
    started = "started"
    completed = "completed"
    failed = "failed"


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
    __table_args__ = (Index("ix_documents_arxiv_id", "arxiv_id"), {"schema": "agent"})

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
    active_index_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.index_versions.id", use_alter=True)
    )
    index_versions: Mapped[list[IndexVersion]] = relationship(
        back_populates="document", foreign_keys="IndexVersion.document_id"
    )


class IndexVersion(TimestampMixin, Base):
    __tablename__ = "index_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_key", name="uq_index_document_version"),
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
    document: Mapped[Document] = relationship(
        back_populates="index_versions", foreign_keys=[document_id]
    )


class IngestionJob(TimestampMixin, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "created_at"), {"schema": "agent"})

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
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    job: Mapped[IngestionJob] = relationship()


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


class ModelCall(TimestampMixin, Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        CheckConstraint(
            "(query_run_id IS NULL) <> (ingestion_attempt_id IS NULL)",
            name="ck_model_call_exactly_one_owner",
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
