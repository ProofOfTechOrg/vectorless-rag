from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vectorless_rag.models import (
    DocumentStatus,
    IndexLifecycleStatus,
    IngestionMode,
    JobStatus,
    ProviderAvailability,
    QueryRunStatus,
    RunRoute,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetadataConstraints(StrictModel):
    arxiv_ids: list[str] = Field(default_factory=list, max_length=50)
    authors: list[str] = Field(default_factory=list, max_length=20)
    topics: list[str] = Field(default_factory=list, max_length=20)
    date_from: date | None = None
    date_to: date | None = None


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20_000)
    thread_id: str | None = Field(default=None, min_length=8, max_length=128)
    constraints: MetadataConstraints = Field(default_factory=MetadataConstraints)

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()


class Citation(StrictModel):
    source_type: Literal["document", "sql"]
    source_id: str
    title: str
    node_id: str | None = None
    node_ids: list[str] = Field(default_factory=list)
    section_paths: list[list[str]] = Field(default_factory=list)
    content_truncated: bool = False
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    retrieval_reason: str | None = None
    sql_audit_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def valid_document_locator(self) -> Citation:
        if self.source_type == "document":
            if self.page_start is None or self.page_end is None:
                raise ValueError("document citations require a page range")
            if self.page_end < self.page_start:
                raise ValueError("citation page range is reversed")
        elif self.sql_audit_id is None:
            raise ValueError("SQL citations require an audit ID")
        return self


class ChatResponse(StrictModel):
    answer: str
    thread_id: str
    route: RunRoute
    trace_id: str
    citations: list[Citation]
    sql_audit_id: uuid.UUID | None = None
    insufficient: bool


class RouteDecision(StrictModel):
    route: RunRoute
    reason: str
    standalone_question: str = Field(min_length=1, max_length=20_000)
    clarification_question: str | None = None


class DocumentMetadata(StrictModel):
    title: str
    authors: list[str]
    abstract: str
    submitted_date: date | None = None
    topics: list[str]
    description: str


class CandidateSelection(StrictModel):
    document_ids: list[uuid.UUID] = Field(max_length=8)
    reasons: dict[str, str] = Field(default_factory=dict)


class NodeSelection(StrictModel):
    selections: list[NodeChoice] = Field(max_length=32)


class NodeChoice(StrictModel):
    document_id: uuid.UUID
    node_id: str
    reason: str


class SufficiencyDecision(StrictModel):
    sufficient: bool
    missing: str | None = None


class SynthesisResult(StrictModel):
    answer: str
    insufficient: bool = False


class SQLPlan(StrictModel):
    sql: str = Field(min_length=1, max_length=20_000)
    explanation: str


class Evidence(StrictModel):
    source_type: Literal["document", "sql"]
    source_id: str
    title: str
    node_id: str | None = None
    node_ids: list[str] = Field(default_factory=list)
    section_paths: list[list[str]] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    content: str
    retrieval_reason: str
    content_truncated: bool = False


class DocumentSummary(StrictModel):
    id: uuid.UUID
    arxiv_id: str | None
    title: str | None
    authors: list[str]
    abstract: str | None
    topics: list[str]
    submitted_date: date | None
    status: DocumentStatus
    page_count: int | None
    diagnostic: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class DocumentPage(StrictModel):
    items: list[DocumentSummary]
    next_cursor: str | None


class IdentityResponse(StrictModel):
    name: str
    scopes: list[str]


class SyncRequest(StrictModel):
    arxiv_ids: list[str] | None = Field(default=None, max_length=100)
    mode: IngestionMode = IngestionMode.ensure_current
    force: bool | None = None

    @model_validator(mode="after")
    def translate_force_alias(self) -> SyncRequest:
        if self.force and self.mode == IngestionMode.ensure_current:
            self.mode = IngestionMode.rebuild_index
        return self


class SyncResponse(StrictModel):
    discovered: int
    queued: int
    deduplicated: int
    pilot_limit_reached: bool
    pilot_limit: int | None = None
    registered_count: int = 0
    remaining_capacity: int | None = None


class JobResponse(StrictModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: JobStatus
    attempts: int
    mode: IngestionMode
    current_stage: str | None = None
    provider_wait_reason: str | None = None
    active_index_version_id: uuid.UUID | None = None
    diagnostic: str | None
    metrics: dict[str, object]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ThreadRun(StrictModel):
    trace_id: str
    route: RunRoute | None
    question: str
    answer: str | None
    citations: list[dict[str, object]]
    insufficient: bool
    status: QueryRunStatus
    failure_code: (
        Literal[
            "deadline_exceeded",
            "request_failed",
            "provider_unavailable",
            "upstream_invalid_response",
        ]
        | None
    ) = None
    created_at: datetime


class ThreadResponse(StrictModel):
    thread_id: str
    runs: list[ThreadRun]


class ThreadSummary(StrictModel):
    thread_id: str
    title: str
    run_count: int
    last_activity_at: datetime


class ThreadPage(StrictModel):
    items: list[ThreadSummary]
    next_cursor: str | None


class FeedbackRequest(StrictModel):
    value: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2_000)


class ProviderStatusResponse(StrictModel):
    availability: ProviderAvailability
    reason: str | None
    checked_at: datetime
    stale: bool


class IndexVersionSummary(StrictModel):
    id: uuid.UUID
    document_id: uuid.UUID
    artifact_schema_version: int
    recipe_key: str
    configuration_hash: str | None
    manifest_sha256: str | None
    pageindex_description: str | None
    page_count: int | None
    node_count: int | None
    max_depth: int | None
    quality: dict[str, object]
    lifecycle_status: IndexLifecycleStatus
    verified_at: datetime | None
    created_at: datetime
    active: bool = False

    model_config = ConfigDict(from_attributes=True, extra="forbid")
