from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import time
from dataclasses import dataclass
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
    DocumentMetadataVersion,
    DocumentStatus,
    IndexActivation,
    IndexActivationReason,
    IndexLifecycleStatus,
    IndexVersion,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    IngestionMode,
    JobStatus,
    ModelCall,
    ModelCallOutcome,
    ModelCallStage,
)
from vectorless_rag.pageindex_adapter import PageIndexAdapter, PageIndexError, artifact_version_key
from vectorless_rag.pageindex_artifact import (
    PageIndexBuildV2,
    configuration_digest,
    load_manifest_v2,
    store_build_v2,
)
from vectorless_rag.provider import persist_provider_interruption
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

HARD_PROVIDER_INTERRUPTION_CODES = frozenset(
    {
        "insufficient_credit",
        "provider_unavailable",
        "pageindex_authentication_failed",
        "pageindex_permission_denied",
        "pageindex_provider_unavailable",
    }
)
TRANSIENT_PROVIDER_INTERRUPTION_CODES = frozenset(
    {
        "network_error",
        "rate_limited",
        "provider_server_error",
        "pageindex_network_error",
        "pageindex_rate_limited",
        "pageindex_insufficient_system_resource",
        "pageindex_llm_retry_exhausted",
        "pageindex_provider_server_error",
        "pageindex_transient_provider_error",
    }
)
SAFE_METRIC_VALUE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")


class UnsupportedPDF(ValueError):
    def __init__(self, failure_code: str) -> None:
        diagnostic = UNSUPPORTED_PDF_DIAGNOSTICS[failure_code]
        super().__init__(diagnostic)
        self.failure_code = failure_code
        self.diagnostic = diagnostic


class IngestionRetryConflict(ValueError):
    pass


class IngestionLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedIngestion:
    job_id: UUID
    attempt_id: UUID
    worker_id: str
    lease_token: UUID
    document_id: UUID
    document_sha256: str
    pdf_object_key: str
    original_filename: str
    mode: IngestionMode
    version_key: str
    prior_index_version_id: UUID | None
    reusable_index_version_id: UUID | None
    metadata_version_id: UUID | None
    metadata: DocumentMetadata | None
    metadata_page_count: int | None
    metadata_extraction_quality: float | None
    metadata_recipe: str
    trace_id: str
    started: float


@dataclass(frozen=True)
class CompletedBuild:
    metadata_version_id: UUID | None
    metadata: DocumentMetadata
    page_count: int
    extraction_quality: float
    artifact: PageIndexBuildV2
    artifact_bytes: int


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


def metadata_recipe_key(settings: Settings) -> str:
    return configuration_digest(
        {
            "prompt_version": settings.metadata_prompt_version,
            "model": settings.deepseek_model,
        }
    )


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
    mode: IngestionMode = IngestionMode.ensure_current,
    experiment_id: UUID | None = None,
) -> tuple[Document, IngestionJob | None, bool]:
    if force and mode == IngestionMode.ensure_current:
        mode = IngestionMode.rebuild_index
    force = force or mode != IngestionMode.ensure_current
    validate_pdf(data, filename, settings.upload_max_bytes)
    digest = sha256_bytes(data)
    existing = await session.scalar(select(Document).where(Document.sha256 == digest))
    if existing is not None:
        if not force:
            return existing, None, True
        pending = await session.scalar(
            select(IngestionJob).where(
                IngestionJob.document_id == existing.id,
                IngestionJob.is_active.is_(True),
            )
        )
        if pending is not None:
            return existing, pending, True
        job = IngestionJob(
            document_id=existing.id,
            force=True,
            mode=mode,
            experiment_id=experiment_id,
            is_active=True,
        )
        session.add(job)
        if existing.active_index_version_id is None:
            existing.status = DocumentStatus.queued
        else:
            existing.status = DocumentStatus.ready
            existing.diagnostic = None
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
    job = IngestionJob(
        document_id=document.id,
        force=force,
        mode=mode,
        experiment_id=experiment_id,
        is_active=True,
    )
    session.add(job)
    await session.flush()
    return document, job, False


async def sync_corpus(
    session: AsyncSession,
    store: ObjectStore,
    settings: Settings,
    *,
    force: bool,
    mode: IngestionMode = IngestionMode.ensure_current,
    arxiv_ids: list[str] | None = None,
    experiment_id: UUID | None = None,
) -> SyncResponse:
    corpus = settings.arxiv_pdf_dir
    if not corpus.is_dir():
        raise FileNotFoundError(f"configured ARXIV_PDF_DIR does not exist: {corpus}")
    pdfs = sorted(
        path for path in corpus.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"
    )
    if arxiv_ids is not None:
        requested = set(arxiv_ids)
        pdfs = [path for path in pdfs if arxiv_id_from_filename(path.name) in requested]
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
            session,
            store,
            data,
            path.name,
            settings,
            force=force,
            mode=mode,
            experiment_id=experiment_id,
        )
        deduplicated += int(duplicate)
        queued += int(job is not None)
        newly_registered += int(not duplicate)
    return SyncResponse(
        discovered=discovered,
        queued=queued,
        deduplicated=deduplicated,
        pilot_limit_reached=pilot_limited and discovered > newly_registered + deduplicated,
        pilot_limit=settings.pilot_document_limit if pilot_limited else None,
        registered_count=(await session.scalar(select(func.count()).select_from(Document)) or 0),
        remaining_capacity=(
            max(
                0,
                settings.pilot_document_limit
                - (await session.scalar(select(func.count()).select_from(Document)) or 0),
            )
            if pilot_limited
            else None
        ),
    )


async def retry_failed_ingestion(session: AsyncSession, failed_job: IngestionJob) -> IngestionJob:
    if failed_job.status != JobStatus.failed:
        raise IngestionRetryConflict("only failed ingestion jobs can be retried")
    pending = await session.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.document_id == failed_job.document_id,
            IngestionJob.is_active.is_(True),
        )
        .with_for_update()
    )
    if pending is not None:
        raise IngestionRetryConflict("the document already has queued or running ingestion work")
    document = await session.get(Document, failed_job.document_id, with_for_update=True)
    if document is None:
        raise IngestionRetryConflict("the ingestion document no longer exists")
    replacement = IngestionJob(
        document_id=document.id,
        status=JobStatus.queued,
        force=True,
        mode=IngestionMode.rebuild_index,
        is_active=True,
    )
    session.add(replacement)
    if document.active_index_version_id is None:
        document.status = DocumentStatus.queued
    else:
        document.status = DocumentStatus.ready
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


async def enqueue_document_reindex(
    session: AsyncSession,
    document_id: UUID,
    *,
    mode: IngestionMode = IngestionMode.rebuild_index,
) -> IngestionJob:
    document = await session.get(Document, document_id, with_for_update=True)
    if document is None:
        raise IngestionRetryConflict("document not found")
    active = await session.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.document_id == document_id,
            IngestionJob.is_active.is_(True),
        )
        .with_for_update()
        .limit(1)
    )
    if active is not None:
        return active
    job = IngestionJob(
        document_id=document_id,
        status=JobStatus.queued,
        force=True,
        mode=mode,
        is_active=True,
    )
    session.add(job)
    if document.active_index_version_id is None:
        document.status = DocumentStatus.queued
    document.diagnostic = None
    await session.flush()
    return job


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

    async def prepare_claim(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        attempt_id: UUID,
        worker_id: str,
        lease_token: UUID,
    ) -> PreparedIngestion:
        job = await session.get(IngestionJob, job_id, with_for_update=True)
        attempt = await session.get(IngestionAttempt, attempt_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            job is None
            or attempt is None
            or job.status != JobStatus.running
            or attempt.status != IngestionAttemptStatus.running
            or job.worker_id != worker_id
            or job.lease_token != lease_token
            or attempt.lease_token != lease_token
            or job.active_attempt_id != attempt_id
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            raise IngestionLeaseLost("ingestion lease is no longer valid")
        document = await session.get(Document, job.document_id, with_for_update=True)
        if document is None:
            raise RuntimeError("ingestion document no longer exists")
        mode = job.mode or (
            IngestionMode.rebuild_index if job.force else IngestionMode.ensure_current
        )
        version_key = artifact_version_key(
            document.sha256,
            self.settings.pageindex_version,
            self.settings.metadata_prompt_version,
            self.settings.pageindex_model,
        )
        metadata_recipe = metadata_recipe_key(self.settings)
        reusable_id: UUID | None = None
        if mode == IngestionMode.ensure_current:
            reusable_id = await session.scalar(
                select(IndexVersion.id)
                .join(
                    DocumentMetadataVersion,
                    IndexVersion.metadata_version_id == DocumentMetadataVersion.id,
                )
                .where(
                    IndexVersion.document_id == document.id,
                    IndexVersion.version_key == version_key,
                    IndexVersion.artifact_schema_version == 2,
                    DocumentMetadataVersion.recipe_key == metadata_recipe,
                    DocumentMetadataVersion.configuration_hash == metadata_recipe,
                    IndexVersion.lifecycle_status.in_(
                        [IndexLifecycleStatus.verified, IndexLifecycleStatus.active]
                    ),
                    IndexVersion.verified_at.is_not(None),
                )
                .order_by(IndexVersion.verified_at.desc(), IndexVersion.created_at.desc())
                .limit(1)
            )
        metadata_version: DocumentMetadataVersion | None = None
        if reusable_id is None and mode != IngestionMode.reextract_all:
            if document.active_index_version_id is not None:
                active_metadata_id = await session.scalar(
                    select(IndexVersion.metadata_version_id).where(
                        IndexVersion.id == document.active_index_version_id,
                        IndexVersion.document_id == document.id,
                    )
                )
                if active_metadata_id is not None:
                    candidate = await session.get(DocumentMetadataVersion, active_metadata_id)
                    if (
                        candidate is not None
                        and candidate.document_id == document.id
                        and candidate.recipe_key == metadata_recipe
                        and candidate.configuration_hash == metadata_recipe
                    ):
                        metadata_version = candidate
            if metadata_version is None:
                metadata_version = await session.scalar(
                    select(DocumentMetadataVersion)
                    .where(
                        DocumentMetadataVersion.document_id == document.id,
                        DocumentMetadataVersion.recipe_key == metadata_recipe,
                        DocumentMetadataVersion.configuration_hash == metadata_recipe,
                    )
                    .order_by(DocumentMetadataVersion.created_at.desc())
                    .limit(1)
                )
        metadata = (
            DocumentMetadata.model_validate(metadata_version.metadata_payload)
            if metadata_version is not None
            else None
        )
        if document.active_index_version_id is None:
            document.status = DocumentStatus.indexing
        job.current_stage = "reuse_check" if reusable_id is not None else "building"
        return PreparedIngestion(
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id=worker_id,
            lease_token=lease_token,
            document_id=document.id,
            document_sha256=document.sha256,
            pdf_object_key=document.pdf_object_key,
            original_filename=document.original_filename,
            mode=mode,
            version_key=version_key,
            prior_index_version_id=document.active_index_version_id,
            reusable_index_version_id=reusable_id,
            metadata_version_id=metadata_version.id if metadata_version is not None else None,
            metadata=metadata,
            metadata_page_count=(
                metadata_version.page_count if metadata_version is not None else None
            ),
            metadata_extraction_quality=(
                metadata_version.extraction_quality if metadata_version is not None else None
            ),
            metadata_recipe=metadata_recipe,
            trace_id=attempt.trace_id,
            started=time.monotonic(),
        )

    async def build_claim(self, prepared: PreparedIngestion) -> CompletedBuild | None:
        if prepared.reusable_index_version_id is not None:
            return None
        pdf_data = await self.store.get_bytes(prepared.pdf_object_key)
        if prepared.metadata is None:
            with usage_context(
                UsageOwner(
                    trace_id=prepared.trace_id,
                    ingestion_attempt_id=prepared.attempt_id,
                )
            ):
                metadata, page_count, quality = await extract_metadata(
                    pdf_data,
                    prepared.original_filename,
                    self.llm,
                    self.settings,
                )
            metadata_version_id = None
        else:
            if (
                prepared.metadata_version_id is None
                or prepared.metadata_page_count is None
                or prepared.metadata_extraction_quality is None
            ):
                raise RuntimeError("prepared metadata snapshot is incomplete")
            metadata = prepared.metadata
            page_count = prepared.metadata_page_count
            quality = prepared.metadata_extraction_quality
            metadata_version_id = prepared.metadata_version_id
        metadata_payload = metadata.model_dump(mode="json")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            await asyncio.to_thread(handle.write, pdf_data)
            await asyncio.to_thread(handle.flush)
            artifact = await self.pageindex.build_v2(
                Path(handle.name),
                str(prepared.document_id),
                prepared.document_sha256,
                metadata_payload,
                ingestion_attempt_id=prepared.attempt_id,
                trace_id=prepared.trace_id,
            )
        await store_build_v2(self.store, artifact)
        await load_manifest_v2(
            self.store,
            artifact.manifest_object_key,
            artifact.manifest_sha256,
        )
        artifact_bytes = (
            len(artifact.raw_bytes)
            + len(artifact.manifest_bytes)
            + sum(len(value) for _, value in artifact.page_objects)
        )
        return CompletedBuild(
            metadata_version_id=metadata_version_id,
            metadata=metadata,
            page_count=page_count,
            extraction_quality=quality,
            artifact=artifact,
            artifact_bytes=artifact_bytes,
        )

    async def verify_reusable(self, session: AsyncSession, prepared: PreparedIngestion) -> None:
        if prepared.reusable_index_version_id is None:
            return
        version = await session.get(IndexVersion, prepared.reusable_index_version_id)
        if (
            version is None
            or version.document_id != prepared.document_id
            or version.manifest_object_key is None
            or version.manifest_sha256 is None
            or version.artifact_schema_version != 2
        ):
            raise RuntimeError("reusable PageIndex version is invalid")
        object_key = version.manifest_object_key
        digest = version.manifest_sha256
        await session.rollback()
        await load_manifest_v2(self.store, object_key, digest)

    async def activate_claim(
        self,
        session: AsyncSession,
        prepared: PreparedIngestion,
        build: CompletedBuild | None,
    ) -> UUID:
        job = await session.get(IngestionJob, prepared.job_id, with_for_update=True)
        attempt = await session.get(IngestionAttempt, prepared.attempt_id, with_for_update=True)
        document = await session.get(Document, prepared.document_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            job is None
            or attempt is None
            or document is None
            or job.status != JobStatus.running
            or attempt.status != IngestionAttemptStatus.running
            or job.worker_id != prepared.worker_id
            or job.lease_token != prepared.lease_token
            or attempt.lease_token != prepared.lease_token
            or job.active_attempt_id != prepared.attempt_id
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            raise IngestionLeaseLost("stale ingestion worker cannot activate an index")

        if build is None:
            version = await session.get(IndexVersion, prepared.reusable_index_version_id)
            if (
                version is None
                or version.document_id != document.id
                or version.artifact_schema_version != 2
                or version.lifecycle_status
                not in (IndexLifecycleStatus.verified, IndexLifecycleStatus.active)
                or version.verified_at is None
            ):
                raise RuntimeError("reusable PageIndex version is no longer valid")
            attempt.artifact_action = ArtifactAction.reused
            reason = IndexActivationReason.reuse
        else:
            metadata_payload = build.metadata.model_dump(mode="json")
            metadata_sha256 = configuration_digest(metadata_payload)
            if build.metadata_version_id is not None:
                metadata_version = await session.get(
                    DocumentMetadataVersion,
                    build.metadata_version_id,
                )
                if (
                    metadata_version is None
                    or metadata_version.document_id != document.id
                    or metadata_version.recipe_key != prepared.metadata_recipe
                    or metadata_version.configuration_hash != prepared.metadata_recipe
                    or metadata_version.content_sha256 != metadata_sha256
                    or metadata_version.page_count != build.page_count
                    or metadata_version.extraction_quality != build.extraction_quality
                ):
                    raise RuntimeError("reusable metadata version is no longer valid")
            else:
                metadata_version = await session.scalar(
                    select(DocumentMetadataVersion).where(
                        DocumentMetadataVersion.document_id == document.id,
                        DocumentMetadataVersion.recipe_key == prepared.metadata_recipe,
                        DocumentMetadataVersion.content_sha256 == metadata_sha256,
                    )
                )
                if metadata_version is None:
                    metadata_version = DocumentMetadataVersion(
                        document_id=document.id,
                        recipe_key=prepared.metadata_recipe,
                        configuration_hash=prepared.metadata_recipe,
                        content_sha256=metadata_sha256,
                        metadata_payload=metadata_payload,
                        page_count=build.page_count,
                        extraction_quality=build.extraction_quality,
                    )
                    session.add(metadata_version)
                    await session.flush()
            manifest = build.artifact.manifest
            version = await session.scalar(
                select(IndexVersion).where(
                    IndexVersion.document_id == document.id,
                    IndexVersion.recipe_key == manifest.recipe_hash,
                    IndexVersion.manifest_sha256 == build.artifact.manifest_sha256,
                )
            )
            if version is None:
                version = IndexVersion(
                    document_id=document.id,
                    version_key=prepared.version_key,
                    pageindex_version=self.settings.pageindex_version,
                    prompt_version=self.settings.metadata_prompt_version,
                    model_name=self.settings.pageindex_model,
                    artifact_object_key=build.artifact.manifest_object_key,
                    artifact_sha256=build.artifact.manifest_sha256,
                    artifact_schema_version=2,
                    recipe_key=manifest.recipe_hash,
                    configuration_hash=manifest.configuration_hash,
                    pipeline_version=self.settings.pageindex_version,
                    metadata_version_id=metadata_version.id,
                    manifest_object_key=build.artifact.manifest_object_key,
                    manifest_sha256=build.artifact.manifest_sha256,
                    pageindex_description=manifest.description,
                    top_level_outline=[item.model_dump(mode="json") for item in manifest.outline],
                    page_count=manifest.quality.page_count,
                    node_count=manifest.quality.node_count,
                    max_depth=manifest.quality.max_depth,
                    quality=manifest.quality.model_dump(mode="json"),
                    build_attempt_id=attempt.id,
                    lifecycle_status=IndexLifecycleStatus.verified,
                    verified_at=now,
                    extraction_quality=build.extraction_quality,
                    elapsed_seconds=time.monotonic() - prepared.started,
                )
                session.add(version)
                await session.flush()
                attempt.artifact_action = (
                    ArtifactAction.rebuilt
                    if prepared.prior_index_version_id is not None
                    else ArtifactAction.created
                )
            else:
                attempt.artifact_action = ArtifactAction.reused
            attempt.page_count = build.page_count
            attempt.extraction_quality = build.extraction_quality
            attempt.artifact_bytes = build.artifact_bytes
            document.title = build.metadata.title
            document.authors = build.metadata.authors
            document.abstract = build.metadata.abstract
            document.description = build.metadata.description
            document.topics = build.metadata.topics
            document.submitted_date = build.metadata.submitted_date
            document.page_count = build.page_count
            reason = (
                IndexActivationReason.reuse
                if attempt.artifact_action == ArtifactAction.reused
                else IndexActivationReason.ingestion
            )

        prior = document.active_index_version_id
        if prior != version.id:
            session.add(
                IndexActivation(
                    document_id=document.id,
                    prior_index_version_id=prior,
                    index_version_id=version.id,
                    reason=reason,
                    ingestion_attempt_id=attempt.id,
                    actor=f"worker:{prepared.worker_id}"[:128],
                )
            )
        document.active_index_version_id = version.id
        document.status = DocumentStatus.ready
        document.diagnostic = None
        attempt.index_version_id = version.id
        job.active_index_version_id = version.id
        await self._finish(
            session,
            job,
            attempt,
            JobStatus.completed,
            IngestionAttemptStatus.completed,
            None,
            prepared.started,
        )
        return version.id

    async def fail_claim(
        self,
        session: AsyncSession,
        prepared: PreparedIngestion,
        error: BaseException,
    ) -> None:
        job = await session.get(IngestionJob, prepared.job_id, with_for_update=True)
        attempt = await session.get(IngestionAttempt, prepared.attempt_id, with_for_update=True)
        document = await session.get(Document, prepared.document_id, with_for_update=True)
        if job is None or attempt is None or document is None:
            return
        if (
            job.worker_id != prepared.worker_id
            or job.lease_token != prepared.lease_token
            or job.active_attempt_id != prepared.attempt_id
        ):
            return
        finish_reason: str | None = None
        llm_attempts = 0
        if isinstance(error, asyncio.CancelledError):
            attempt_status = IngestionAttemptStatus.abandoned
            failure_code = "worker_cancelled"
            diagnostic = "Ingestion was interrupted and will be retried."
            job_status = JobStatus.queued
            attempt.retry_budget_consumed = False
        elif isinstance(error, UnsupportedPDF):
            attempt_status = IngestionAttemptStatus.unsupported
            failure_code = error.failure_code
            diagnostic = error.diagnostic
            job_status = JobStatus.unsupported
        elif getattr(error, "failure_code", None) == "experiment_cost_cap":
            attempt_status = IngestionAttemptStatus.deferred
            failure_code = "experiment_cost_cap"
            diagnostic = "The frozen experiment cost cap was reached before the next call."
            job_status = JobStatus.deferred
            attempt.retry_budget_consumed = False
        else:
            failure_code = "ingestion_failed"
            diagnostic = "Ingestion failed unexpectedly."
            retryable = False
            if isinstance(error, (PageIndexError, LLMError)):
                failure_code = error.failure_code
                diagnostic = error.diagnostic
                retryable = getattr(error, "retryable", False)
                finish_reason = error.finish_reason
                llm_attempts = error.attempts
            hard_provider_interruption = failure_code in HARD_PROVIDER_INTERRUPTION_CODES
            transient_provider_interruption = failure_code in TRANSIENT_PROVIDER_INTERRUPTION_CODES
            if hard_provider_interruption or transient_provider_interruption:
                attempt_status = IngestionAttemptStatus.deferred
                job_status = JobStatus.waiting_provider
                attempt.retry_budget_consumed = transient_provider_interruption
                job.provider_wait_reason = failure_code
                await persist_provider_interruption(
                    session,
                    self.settings,
                    failure_code,
                )
            else:
                attempt_status = IngestionAttemptStatus.failed
                job_status = JobStatus.failed
                attempt.retry_budget_consumed = not retryable
        if attempt.retry_budget_consumed:
            job.retry_budget_consumed += 1
        document.status = (
            DocumentStatus.ready
            if document.active_index_version_id is not None
            else (
                DocumentStatus.unsupported
                if job_status == JobStatus.unsupported
                else (
                    DocumentStatus.waiting_provider
                    if job_status == JobStatus.waiting_provider
                    else (
                        DocumentStatus.deferred
                        if job_status == JobStatus.deferred
                        else DocumentStatus.failed
                    )
                )
            )
        )
        document.diagnostic = None if document.status == DocumentStatus.ready else diagnostic
        await self._finish(
            session,
            job,
            attempt,
            job_status,
            attempt_status,
            failure_code,
            prepared.started,
            diagnostic=diagnostic,
        )
        failure_metrics: dict[str, object] = {"failure_code": failure_code}
        if finish_reason is not None and SAFE_METRIC_VALUE.fullmatch(finish_reason):
            failure_metrics["provider_finish_reason"] = finish_reason
        if 0 < llm_attempts <= 100:
            failure_metrics["llm_attempts"] = llm_attempts
        job.metrics = {**job.metrics, **failure_metrics}
        if job_status in (JobStatus.queued, JobStatus.waiting_provider):
            job.is_active = True

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
        job.is_active = job_status in (JobStatus.queued, JobStatus.waiting_provider)
        job.worker_id = None
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.active_attempt_id = None
        job.current_stage = job_status.value
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
