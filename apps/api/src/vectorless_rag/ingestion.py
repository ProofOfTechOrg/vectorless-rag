from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

import fitz
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings
from vectorless_rag.llm import UNTRUSTED_DOCUMENT_SYSTEM, StructuredLLM, wrap_untrusted_document
from vectorless_rag.models import (
    Document,
    DocumentStatus,
    IndexVersion,
    IngestionJob,
    JobStatus,
)
from vectorless_rag.pageindex_adapter import PageIndexAdapter, artifact_version_key
from vectorless_rag.schemas import DocumentMetadata, SyncResponse
from vectorless_rag.storage import ObjectStore

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


class UnsupportedPDF(ValueError):
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
        raise UnsupportedPDF("only .pdf files are accepted")
    if len(data) > maximum_bytes:
        raise UnsupportedPDF(f"PDF exceeds the {maximum_bytes} byte limit")
    if not data.startswith(b"%PDF-"):
        raise UnsupportedPDF("file does not have a PDF signature")


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
        raise UnsupportedPDF(f"PDF parsing failed: {exc}") from exc
    combined = "\n\n".join(texts).strip()
    non_space = sum(not char.isspace() for char in combined)
    quality = min(1.0, non_space / max(1, min(page_count, count) * 1_500))
    return combined, page_count, quality, embedded_title


async def extract_metadata(
    data: bytes, filename: str, llm: StructuredLLM, settings: Settings
) -> tuple[DocumentMetadata, int, float]:
    text, page_count, quality, embedded_title = await asyncio.to_thread(extract_first_pages, data)
    if len(text) < settings.extraction_min_characters:
        raise UnsupportedPDF(
            f"extraction-poor PDF: first pages yielded {len(text)} characters; OCR is not enabled"
        )
    arxiv_id = arxiv_id_from_filename(filename)
    prompt = (
        "Extract bibliographic metadata from these first PDF pages. Do not invent missing values. "
        f"The filename-derived ArXiv ID is {arxiv_id or 'unknown'}.\n"
        + wrap_untrusted_document(text[:60_000])
    )
    metadata = await llm.structured(
        DocumentMetadata, UNTRUSTED_DOCUMENT_SYSTEM, prompt, thinking=False
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

    async def process(self, session: AsyncSession, job: IngestionJob) -> None:
        started = time.monotonic()
        document = await session.get(Document, job.document_id, with_for_update=True)
        if document is None:
            raise RuntimeError(f"document {job.document_id} no longer exists")
        document.status = DocumentStatus.indexing
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
                self._finish(job, JobStatus.completed, "existing artifact reused", started)
                return
            metadata_payload = metadata.model_dump(mode="json")
            with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
                await asyncio.to_thread(handle.write, pdf_data)
                await asyncio.to_thread(handle.flush)
                artifact = await self.pageindex.index(
                    Path(handle.name), str(document.id), metadata_payload
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
            if existing is None:
                session.add(index)
            await session.flush()
            document.active_index_version_id = index.id
            document.status = DocumentStatus.ready
            document.diagnostic = None
            self._finish(job, JobStatus.completed, None, started)
            job.metrics = {
                "page_count": page_count,
                "extraction_quality": quality,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "artifact_bytes": len(artifact),
            }
        except UnsupportedPDF as exc:
            document.status = DocumentStatus.unsupported
            document.diagnostic = str(exc)
            self._finish(job, JobStatus.unsupported, str(exc), started)
        except Exception as exc:
            document.status = DocumentStatus.failed
            document.diagnostic = str(exc)[:4000]
            self._finish(job, JobStatus.failed, str(exc)[:4000], started)
            raise

    @staticmethod
    def _finish(
        job: IngestionJob, status: JobStatus, diagnostic: str | None, started: float
    ) -> None:
        job.status = status
        job.diagnostic = diagnostic
        job.completed_at = datetime.now(UTC)
        job.metrics = {**job.metrics, "elapsed_seconds": round(time.monotonic() - started, 3)}


def build_pilot_report(
    jobs: list[IngestionJob],
    *,
    total_documents: int = 537,
    input_cost_per_million_usd: float | None = None,
    output_cost_per_million_usd: float | None = None,
) -> dict[str, object]:
    completed = [job for job in jobs if job.status == JobStatus.completed]
    elapsed = sum(float(job.metrics.get("elapsed_seconds", 0)) for job in completed)
    input_tokens = sum(int(job.metrics.get("input_tokens", 0)) for job in completed)
    output_tokens = sum(int(job.metrics.get("output_tokens", 0)) for job in completed)
    projection_factor = total_documents / max(1, len(completed))
    projected_input = round(input_tokens * projection_factor)
    projected_output = round(output_tokens * projection_factor)
    projected_spend = None
    if input_cost_per_million_usd is not None and output_cost_per_million_usd is not None:
        projected_spend = round(
            projected_input * input_cost_per_million_usd / 1_000_000
            + projected_output * output_cost_per_million_usd / 1_000_000,
            2,
        )
    return {
        "documents": len(jobs),
        "completed": len(completed),
        "unsupported": sum(job.status == JobStatus.unsupported for job in jobs),
        "failed": sum(job.status == JobStatus.failed for job in jobs),
        "elapsed_seconds": elapsed,
        "average_extraction_quality": (
            sum(float(job.metrics.get("extraction_quality", 0)) for job in completed)
            / max(1, len(completed))
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "projected_full_corpus_input_tokens": projected_input,
        "projected_full_corpus_output_tokens": projected_output,
        "projected_full_corpus_spend_usd": projected_spend,
        "full_corpus_approved": False,
        "note": "Set ALLOW_FULL_CORPUS_INDEX=true only after explicit approval.",
    }
