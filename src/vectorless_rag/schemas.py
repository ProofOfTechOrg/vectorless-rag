from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vectorless_rag.models import DocumentStatus, JobStatus, RunRoute


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
    citation_ids: list[str]
    insufficient: bool = False


class SQLPlan(StrictModel):
    sql: str = Field(min_length=1, max_length=20_000)
    explanation: str


class Evidence(StrictModel):
    source_type: Literal["document", "sql"]
    source_id: str
    title: str
    node_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    content: str
    retrieval_reason: str


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


class SyncRequest(StrictModel):
    force: bool = False


class SyncResponse(StrictModel):
    discovered: int
    queued: int
    deduplicated: int
    pilot_limit_reached: bool


class JobResponse(StrictModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: JobStatus
    attempts: int
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
    created_at: datetime


class ThreadResponse(StrictModel):
    thread_id: str
    runs: list[ThreadRun]


class FeedbackRequest(StrictModel):
    value: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2_000)
