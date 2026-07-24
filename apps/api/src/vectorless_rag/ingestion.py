from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

import fitz
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings
from vectorless_rag.llm import (
    UNTRUSTED_DOCUMENT_SYSTEM,
    LLMError,
    StructuredLLM,
    wrap_untrusted_document,
)
from vectorless_rag.models import (
    ArtifactAction,
    Document,
    DocumentStatus,
    IndexVersion,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    JobStatus,
    ModelCall,
    ModelCallOutcome,
    ModelCallStage,
)
from vectorless_rag.pageindex_adapter import PageIndexAdapter, PageIndexError, artifact_version_key
from vectorless_rag.reporting import build_pilot_report as build_pilot_report
from vectorless_rag.schemas import DocumentMetadata, SyncResponse
from vectorless_rag.storage import ObjectStore
from vectorless_rag.usage import UsageOwner, usage_context

ARXIV_FILENAME = re.compile(r"^(?P<id>\d{4}\.\d{4,5})(?:v\d+)?\.pdf$", re.IGNORECASE)
PLACEHOLDER_TITLES = frozenset(
    {
        "",
        "n/a",
        "none",
        "not available",
        "null",
        "unknown",
        "unknown title",
        "untitled",
        "untitled document",
    }
)


UNSUPPORTED_PDF_DIAGNOSTICS = {
    "unsupported_file_type": "Only PDF files are accepted.",
    "pdf_too_large": "The PDF exceeds the configured upload limit.",
    "invalid_pdf_signature": "The file does not have a PDF signature.",
    "pdf_parsing_failed": "The PDF could not be parsed.",
    "pdf_text_extraction_insufficient": (
        "The PDF has insufficient extractable text; OCR is not enabled."
    ),
}


class UnsupportedPDF(ValueError):
    def __init__(self, failure_code: str) -> None:
        diagnostic = UNSUPPORTED_PDF_DIAGNOSTICS[failure_code]
        super().__init__(diagnostic)
        self.failure_code = failure_code
        self.diagnostic = diagnostic


class IngestionRetryConflict(ValueError):
    pass


class _TextPage(Protocol):
    def get_text(self, option: Literal["text"]) -> str: ...


def arxiv_id_from_filename(filename: str) -> str | None:
    match = ARXIV_FILENAME.match(Path(filename).name)
    return match.group("id") if match else None


def normalize_document_title(extracted: str, embedded: str | None, filename: str) -> str:
    for candidate in (extracted, embedded):
        normalized = candidate.strip() if candidate else ""
        if normalized.casefold() not in PLACEHOLDER_TITLES:
            return normalized
    return Path(filename).stem


def validate_pdf(data: bytes, filename: str, maximum_bytes: int) -> None:
    if not filename.lower().endswith(".pdf"):
        raise UnsupportedPDF("unsupported_file_type")
    if len(data) > maximum_bytes:
        raise UnsupportedPDF("pdf_too_large")
    if not data.startswith(b"%PDF-"):
        raise UnsupportedPDF("invalid_pdf_signature")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_first_pages(data: bytes, count: int = 3) -> tuple[str, int, float, str | None]:
    try:
        with fitz.open(stream=data, filetype="pdf") as pdf:
            texts = [
                cast(_TextPage, pdf[index]).get_text("text")
                for index in range(min(count, len(pdf)))
            ]
            page_count = len(pdf)
            pdf_metadata = cast(dict[str, object] | None, pdf.metadata) or {}
            raw_title = pdf_metadata.get("title")
            embedded_title = (raw_title.strip() or None) if isinstance(raw_title, str) else None
    except Exception as exc:
        raise UnsupportedPDF("pdf_parsing_failed") from exc
    combined = "\n\n".join(texts).strip()
    non_space = sum(not char.isspace() for char in combined)
    quality = min(1.0, non_space / max(1, min(page_count, count) * 1_500))
    return combined, page_count, quality, embedded_title


async def extract_metadata(
    data: bytes, filename: str, llm: StructuredLLM, settings: Settings
) -> tuple[DocumentMetadata, int, float]:
    text, page_count, quality, embedded_title = await asyncio.to_thread(extract_first_pages, data)
    if len(text) < settings.extraction_min_characters:
        raise UnsupportedPDF("pdf_text_extraction_insufficient")
    arxiv_id = arxiv_id_from_filename(filename)
    prompt = (
        "Extract bibliographic metadata from these first PDF pages. Do not invent missing values. "
        f"The filename-derived ArXiv ID is {arxiv_id or 'unknown'}.\n"
        + wrap_untrusted_document(text[:60_000])
    )
    metadata = await llm.structured(
        DocumentMetadata,
        UNTRUSTED_DOCUMENT_SYSTEM,
        prompt,
        thinking=False,
        stage=ModelCallStage.ingestion_metadata,
    )
    metadata = metadata.model_copy(
        update={"title": normalize_document_title(metadata.title, embedded_title, filename)}
    )
    return metadata, page_count, quality


async def register_pdf(
    session: AsyncSession,
    store: ObjectStore,
    data: bytes,
    filename: str,
    settings: Settings,
    *,
    force: bool = False,
) -> tuple[Document, IngestionJob | None, bool]:
    validate_pdf(data, filename, settings.upload_max_bytes)
    digest = sha256_bytes(data)
    existing = await session.scalar(select(Document).where(Document.sha256 == digest))
    if existing is not None:
        if not force:
            return existing, None, True
        pending = await session.scalar(
            select(IngestionJob).where(
                IngestionJob.document_id == existing.id,
                IngestionJob.status.in_([JobStatus.queued, JobStatus.running]),
            )
        )
        if pending is not None:
            return existing, pending, True
        job = IngestionJob(document_id=existing.id, force=True)
        session.add(job)
        existing.status = DocumentStatus.queued
        await session.flush()
        return existing, job, True

    object_key = f"pdf/{digest[:2]}/{digest}.pdf"
    await store.put_bytes(object_key, data, "application/pdf")
    document = Document(
        sha256=digest,
        arxiv_id=arxiv_id_from_filename(filename),
        original_filename=Path(filename).name,
        pdf_object_key=object_key,
        status=DocumentStatus.queued,
    )
    session.add(document)
    await session.flush()
    job = IngestionJob(document_id=document.id, force=force)
    session.add(job)
    await session.flush()
    return document, job, False


async def sync_corpus(
    session: AsyncSession, store: ObjectStore, settings: Settings, *, force: bool
) -> SyncResponse:
    corpus = settings.arxiv_pdf_dir
    if not corpus.is_dir():
        raise FileNotFoundError(f"configured ARXIV_PDF_DIR does not exist: {corpus}")
    pdfs = sorted(
        path for path in corpus.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"
    )
    pilot_limited = not settings.allow_full_corpus_index
    capacity = len(pdfs)
    if pilot_limited:
        registered = await session.scalar(select(func.count()).select_from(Document)) or 0
        capacity = max(0, settings.pilot_document_limit - registered)
    discovered = len(pdfs)
    queued = deduplicated = newly_registered = 0
    for path in pdfs:
        data = await asyncio.to_thread(path.read_bytes)
        digest = sha256_bytes(data)
        known = await session.scalar(select(Document.id).where(Document.sha256 == digest))
        if pilot_limited and known is None and newly_registered >= capacity:
            continue
        _, job, duplicate = await register_pdf(
            session, store, data, path.name, settings, force=force
        )
        deduplicated += int(duplicate)
        queued += int(job is not None)
        newly_registered += int(not duplicate)
    return SyncResponse(
        discovered=discovered,
        queued=queued,
        deduplicated=deduplicated,
        pilot_limit_reached=pilot_limited and discovered > newly_registered + deduplicated,
    )


async def retry_failed_ingestion(session: AsyncSession, failed_job: IngestionJob) -> IngestionJob:
    if failed_job.status != JobStatus.failed:
        raise IngestionRetryConflict("only failed ingestion jobs can be retried")
    pending = await session.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.document_id == failed_job.document_id,
            IngestionJob.status.in_([JobStatus.queued, JobStatus.running]),
        )
        .with_for_update()
    )
    if pending is not None:
        raise IngestionRetryConflict("the document already has queued or running ingestion work")
    document = await session.get(Document, failed_job.document_id, with_for_update=True)
    if document is None:
        raise IngestionRetryConflict("the ingestion document no longer exists")
    replacement = IngestionJob(document_id=document.id, status=JobStatus.queued, force=True)
    session.add(replacement)
    document.status = DocumentStatus.queued
    document.diagnostic = None
    await session.flush()
    return replacement


async def retry_latest_document_ingestion(session: AsyncSession, document_id: UUID) -> IngestionJob:
    latest_job = await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.document_id == document_id)
        .order_by(desc(IngestionJob.created_at), desc(IngestionJob.id))
        .with_for_update()
        .limit(1)
    )
    if latest_job is None:
        raise IngestionRetryConflict("the document has no ingestion work to retry")
    return await retry_failed_ingestion(session, latest_job)


class IngestionProcessor:
    def __init__(
        self,
        settings: Settings,
        store: ObjectStore,
        llm: StructuredLLM,
        pageindex: PageIndexAdapter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.llm = llm
        self.pageindex = pageindex

    async def process(
        self,
        session: AsyncSession,
        job: IngestionJob,
        attempt: IngestionAttempt,
    ) -> None:
        started = time.monotonic()
        if attempt.job_id != job.id or attempt.status != IngestionAttemptStatus.running:
            raise RuntimeError("ingestion attempt is no longer active")
        document = await session.get(Document, job.document_id, with_for_update=True)
        if document is None:
            raise RuntimeError("ingestion document no longer exists")
        document.status = DocumentStatus.indexing
        with usage_context(UsageOwner(trace_id=attempt.trace_id, ingestion_attempt_id=attempt.id)):
            try:
                pdf_data = await self.store.get_bytes(document.pdf_object_key)
                metadata, page_count, quality = await extract_metadata(
                    pdf_data, document.original_filename, self.llm, self.settings
                )
                document.title = metadata.title
                document.authors = metadata.authors
                document.abstract = metadata.abstract
                document.description = metadata.description
                document.topics = metadata.topics
                document.submitted_date = metadata.submitted_date
                document.page_count = page_count
                attempt.page_count = page_count
                attempt.extraction_quality = quality
                version_key = artifact_version_key(
                    document.sha256,
                    self.settings.pageindex_version,
                    self.settings.metadata_prompt_version,
                    self.settings.pageindex_model,
                )
                existing = await session.scalar(
                    select(IndexVersion).where(
                        IndexVersion.document_id == document.id,
                        IndexVersion.version_key == version_key,
                    )
                )
                if existing is not None and not job.force:
                    document.active_index_version_id = existing.id
                    document.status = DocumentStatus.ready
                    document.diagnostic = None
                    attempt.artifact_action = ArtifactAction.reused
                    await self._finish(
                        session,
                        job,
                        attempt,
                        JobStatus.completed,
                        IngestionAttemptStatus.completed,
                        None,
                        started,
                    )
                    return
                metadata_payload = metadata.model_dump(mode="json")
                with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
                    await asyncio.to_thread(handle.write, pdf_data)
                    await asyncio.to_thread(handle.flush)
                    artifact = await self.pageindex.index(
                        Path(handle.name),
                        str(document.id),
                        metadata_payload,
                        ingestion_attempt_id=attempt.id,
                        trace_id=attempt.trace_id,
                    )
                artifact_digest = sha256_bytes(artifact)
                artifact_key = f"artifacts/{document.id}/{version_key}.json"
                await self.store.put_bytes(artifact_key, artifact, "application/json")
                index = existing or IndexVersion(
                    document_id=document.id,
                    version_key=version_key,
                    pageindex_version=self.settings.pageindex_version,
                    prompt_version=self.settings.metadata_prompt_version,
                    model_name=self.settings.pageindex_model,
                    artifact_object_key=artifact_key,
                    artifact_sha256=artifact_digest,
                )
                index.artifact_object_key = artifact_key
                index.artifact_sha256 = artifact_digest
                index.extraction_quality = quality
                index.elapsed_seconds = time.monotonic() - started
                attempt.artifact_action = (
                    ArtifactAction.created if existing is None else ArtifactAction.rebuilt
                )
                attempt.artifact_bytes = len(artifact)
                if existing is None:
                    session.add(index)
                await session.flush()
                totals = await self._usage_summary(session, attempt.id)
                index.input_tokens = totals["input_tokens"]
                index.output_tokens = totals["output_tokens"]
                document.active_index_version_id = index.id
                document.status = DocumentStatus.ready
                document.diagnostic = None
                await self._finish(
                    session,
                    job,
                    attempt,
                    JobStatus.completed,
                    IngestionAttemptStatus.completed,
                    None,
                    started,
                    usage=totals,
                )
            except UnsupportedPDF as exc:
                document.status = DocumentStatus.unsupported
                document.diagnostic = exc.diagnostic
                await self._finish(
                    session,
                    job,
                    attempt,
                    JobStatus.unsupported,
                    IngestionAttemptStatus.unsupported,
                    exc.failure_code,
                    started,
                    diagnostic=exc.diagnostic,
                )
            except Exception as exc:
                failure_code = "ingestion_failed"
                diagnostic = "Ingestion failed unexpectedly."
                finish_reason: str | None = None
                attempts = 0
                if isinstance(exc, (PageIndexError, LLMError)):
                    failure_code = exc.failure_code
                    diagnostic = exc.diagnostic
                    finish_reason = exc.finish_reason
                    attempts = exc.attempts
                document.status = DocumentStatus.failed
                document.diagnostic = diagnostic
                await self._finish(
                    session,
                    job,
                    attempt,
                    JobStatus.failed,
                    IngestionAttemptStatus.failed,
                    failure_code,
                    started,
                    diagnostic=diagnostic,
                )
                failure_metrics: dict[str, object] = {"failure_code": failure_code}
                if finish_reason is not None:
                    failure_metrics["provider_finish_reason"] = finish_reason
                if attempts:
                    failure_metrics["llm_attempts"] = attempts
                job.metrics = {**job.metrics, **failure_metrics}
                raise

    @staticmethod
    async def _usage_summary(session: AsyncSession, attempt_id: UUID) -> dict[str, int | bool]:
        calls = list(
            (
                await session.scalars(
                    select(ModelCall).where(ModelCall.ingestion_attempt_id == attempt_id)
                )
            ).all()
        )
        available = [call for call in calls if call.usage_available]
        return {
            "model_call_count": len(calls),
            "input_tokens": sum(call.input_tokens or 0 for call in available),
            "output_tokens": sum(call.output_tokens or 0 for call in available),
            "telemetry_complete": all(
                call.outcome != ModelCallOutcome.started and call.usage_available for call in calls
            ),
        }

    async def _finish(
        self,
        session: AsyncSession,
        job: IngestionJob,
        attempt: IngestionAttempt,
        job_status: JobStatus,
        attempt_status: IngestionAttemptStatus,
        failure_code: str | None,
        started: float,
        *,
        diagnostic: str | None = None,
        usage: dict[str, int | bool] | None = None,
    ) -> None:
        usage = usage or await self._usage_summary(session, attempt.id)
        elapsed = round(time.monotonic() - started, 3)
        job.status = job_status
        job.diagnostic = diagnostic
        job.completed_at = datetime.now(UTC)
        attempt.status = attempt_status
        attempt.failure_code = failure_code
        attempt.completed_at = job.completed_at
        attempt.elapsed_seconds = elapsed
        attempt.telemetry_complete = bool(usage["telemetry_complete"])
        job.metrics = {
            "attempt_id": str(attempt.id),
            "trace_id": attempt.trace_id,
            "elapsed_seconds": elapsed,
            "page_count": attempt.page_count,
            "extraction_quality": attempt.extraction_quality,
            "artifact_bytes": attempt.artifact_bytes,
            "artifact_action": attempt.artifact_action.value,
            "telemetry_complete": attempt.telemetry_complete,
            "model_call_count": usage["model_call_count"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        }
