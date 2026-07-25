import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import fitz
import pytest
from pytest import MonkeyPatch

from vectorless_rag.config import Settings
from vectorless_rag.ingestion import (
    IngestionProcessor,
    IngestionRetryConflict,
    PreparedIngestion,
    UnsupportedPDF,
    arxiv_id_from_filename,
    extract_first_pages,
    metadata_recipe_key,
    normalize_document_title,
    register_pdf,
    retry_failed_ingestion,
    retry_latest_document_ingestion,
    sha256_bytes,
    validate_pdf,
)
from vectorless_rag.models import (
    ArtifactAction,
    Document,
    DocumentMetadataVersion,
    DocumentStatus,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    IngestionMode,
    JobStatus,
    ModelCall,
    ProviderAvailability,
    ProviderState,
)
from vectorless_rag.pageindex_adapter import PageIndexError
from vectorless_rag.pageindex_artifact import PageIndexBuildV2, build_manifest_v2
from vectorless_rag.schemas import DocumentMetadata
from vectorless_rag.usage import (
    ExperimentBudgetExceeded,
    UsageOwner,
    current_usage_owner,
)


class _WritablePage(Protocol):
    def insert_text(self, point: tuple[int, int], text: str) -> int: ...


class _WritableDocument(Protocol):
    def new_page(self) -> _WritablePage: ...

    def set_metadata(self, metadata: dict[str, str]) -> None: ...

    def tobytes(self) -> bytes: ...

    def close(self) -> None: ...


class _RetrySession:
    def __init__(self, document: Document, *, pending: IngestionJob | None = None) -> None:
        self.document = document
        self.pending = pending
        self.added: list[object] = []

    async def scalar(self, statement: object) -> IngestionJob | None:
        del statement
        return self.pending

    async def get(
        self, model: object, identifier: object, *, with_for_update: bool = False
    ) -> Document | None:
        del model, identifier, with_for_update
        return self.document

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _DocumentRetrySession(_RetrySession):
    def __init__(self, document: Document, latest: IngestionJob | None) -> None:
        super().__init__(document)
        self.latest = latest
        self.scalar_calls = 0

    async def scalar(self, statement: object) -> IngestionJob | None:
        del statement
        self.scalar_calls += 1
        return self.latest if self.scalar_calls == 1 else self.pending


class _Rows:
    def __init__(self, rows: Sequence[object] | None = None) -> None:
        self.rows = list(rows or [])

    def all(self) -> list[object]:
        return self.rows


class _ClaimSession:
    def __init__(
        self,
        values: dict[object, object],
        *,
        scalar_values: Sequence[object] | None = None,
        calls: list[ModelCall] | None = None,
    ) -> None:
        self.values = values
        self.scalar_values = deque(scalar_values or [])
        self.calls = calls or []
        self.added: list[object] = []

    async def get(
        self, model: object, identifier: object, *, with_for_update: bool = False
    ) -> object | None:
        del identifier, with_for_update
        return self.values.get(model)

    async def scalar(self, statement: object) -> object | None:
        del statement
        return self.scalar_values.popleft() if self.scalar_values else None

    async def scalars(self, statement: object) -> "_Rows":
        del statement
        return _Rows(self.calls)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _RegisterSession:
    def __init__(self, document: Document) -> None:
        self.document = document
        self.scalar_calls = 0
        self.added: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        del statement
        self.scalar_calls += 1
        return self.document if self.scalar_calls == 1 else None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _Store:
    async def get_bytes(self, key: str) -> bytes:
        del key
        return b"%PDF-private-document"

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        del key, data, content_type


class _V2PageIndex:
    def __init__(self) -> None:
        self.metadata: dict[str, object] | None = None
        self.owner: tuple[uuid.UUID, str] | None = None

    async def build_v2(
        self,
        path: Path,
        document_id: str,
        document_sha256: str,
        metadata: dict[str, object],
        *,
        ingestion_attempt_id: uuid.UUID,
        trace_id: str,
    ) -> PageIndexBuildV2:
        del path
        self.metadata = metadata
        self.owner = ingestion_attempt_id, trace_id
        return build_manifest_v2(
            {
                "doc_description": "A test document.",
                "structure": [
                    {
                        "node_id": "root",
                        "title": "Root",
                        "start_index": 1,
                        "end_index": 1,
                        "summary": "Root summary.",
                    }
                ],
            },
            ["page text"],
            document_id=document_id,
            document_sha256=document_sha256,
            metadata=metadata,
            generator_commit="test",
            patch_sha256="b" * 64,
            model="provider/model",
            prompt_profile="pageindex-v2",
            options_profile={},
            parser="pymupdf",
            parser_version="1",
        )


def _document() -> Document:
    return Document(
        id=uuid.uuid4(),
        sha256="a" * 64,
        original_filename="paper.pdf",
        pdf_object_key="pdf/paper.pdf",
        status=DocumentStatus.failed,
    )


def _running_claim(
    document: Document,
    *,
    mode: IngestionMode = IngestionMode.rebuild_index,
) -> tuple[IngestionJob, IngestionAttempt, PreparedIngestion]:
    lease_token = uuid.uuid4()
    attempt_id = uuid.uuid4()
    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status=JobStatus.running,
        mode=mode,
        force=mode != IngestionMode.ensure_current,
        worker_id="test-worker",
        lease_token=lease_token,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        active_attempt_id=attempt_id,
        retry_budget_consumed=0,
        is_active=True,
        metrics={},
    )
    attempt = IngestionAttempt(
        id=attempt_id,
        job_id=job.id,
        attempt_number=1,
        trace_id=uuid.uuid4().hex,
        worker_id="test-worker",
        status=IngestionAttemptStatus.running,
        artifact_action=ArtifactAction.none,
        lease_token=lease_token,
        retry_budget_consumed=False,
    )
    prepared = PreparedIngestion(
        job_id=job.id,
        attempt_id=attempt.id,
        worker_id="test-worker",
        lease_token=lease_token,
        document_id=document.id,
        document_sha256=document.sha256,
        pdf_object_key=document.pdf_object_key,
        original_filename=document.original_filename,
        mode=mode,
        version_key="v" * 64,
        prior_index_version_id=document.active_index_version_id,
        reusable_index_version_id=None,
        metadata_version_id=None,
        metadata=None,
        metadata_page_count=None,
        metadata_extraction_quality=None,
        metadata_recipe=metadata_recipe_key(Settings()),
        trace_id=attempt.trace_id,
        started=0.0,
    )
    return job, attempt, prepared


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("2504.14489.pdf", "2504.14489"), ("2504.14489v2.pdf", "2504.14489"), ("paper.pdf", None)],
)
def test_arxiv_id_is_derived_only_from_filename(filename: str, expected: str | None) -> None:
    assert arxiv_id_from_filename(filename) == expected


def test_pdf_validation_checks_signature_type_and_size() -> None:
    validate_pdf(b"%PDF-1.7\n", "paper.pdf", 100)
    with pytest.raises(UnsupportedPDF, match="signature"):
        validate_pdf(b"not pdf", "paper.pdf", 100)
    with pytest.raises(UnsupportedPDF, match="exceeds"):
        validate_pdf(b"%PDF-" + b"x" * 100, "paper.pdf", 10)
    with pytest.raises(UnsupportedPDF, match="Only PDF"):
        validate_pdf(b"%PDF-1.7", "paper.txt", 100)


def test_sha256_deduplication_key_is_deterministic() -> None:
    assert sha256_bytes(b"paper") == sha256_bytes(b"paper")
    assert sha256_bytes(b"paper") != sha256_bytes(b"other")


@pytest.mark.parametrize("placeholder", ["", "Unknown", "untitled", "N/A", "None"])
def test_placeholder_title_uses_embedded_pdf_title(placeholder: str) -> None:
    assert normalize_document_title(placeholder, "  A Real Paper  ", "2504.14489.pdf") == (
        "A Real Paper"
    )


def test_placeholder_titles_use_filename_as_last_resort() -> None:
    assert normalize_document_title("Unknown", "Untitled", "2504.14489v2.pdf") == "2504.14489v2"


def test_extracted_title_takes_precedence() -> None:
    assert normalize_document_title("  Extracted Title  ", "Embedded Title", "paper.pdf") == (
        "Extracted Title"
    )


def test_pdf_extraction_reads_embedded_title() -> None:
    document = cast(_WritableDocument, fitz.open())
    page = document.new_page()
    page.insert_text((72, 72), "A text-rich paper page")
    document.set_metadata({"title": "Embedded Paper Title"})
    data = document.tobytes()
    document.close()

    text, page_count, _, embedded_title = extract_first_pages(data)

    assert text == "A text-rich paper page"
    assert page_count == 1
    assert embedded_title == "Embedded Paper Title"


def test_pdf_parser_failures_never_persist_raw_parser_diagnostics() -> None:
    private_fragment = b"%PDF-private-corrupt-fragment"

    with pytest.raises(UnsupportedPDF) as error:
        extract_first_pages(private_fragment)

    assert error.value.failure_code == "pdf_parsing_failed"
    assert str(error.value) == "The PDF could not be parsed."
    assert "private-corrupt-fragment" not in str(error.value)


async def test_retry_requires_failed_job_and_no_pending_work() -> None:
    document = _document()
    completed = IngestionJob(document_id=document.id, status=JobStatus.completed)
    session = _RetrySession(document)

    with pytest.raises(IngestionRetryConflict, match="only failed"):
        await retry_failed_ingestion(cast(Any, session), completed)

    failed = IngestionJob(document_id=document.id, status=JobStatus.failed)
    session.pending = IngestionJob(document_id=document.id, status=JobStatus.running)
    with pytest.raises(IngestionRetryConflict, match="already has"):
        await retry_failed_ingestion(cast(Any, session), failed)


async def test_retry_creates_forced_replacement_and_preserves_failed_history() -> None:
    document = _document()
    failed = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status=JobStatus.failed,
        diagnostic="safe old failure",
    )
    session = _RetrySession(document)

    replacement = await retry_failed_ingestion(cast(Any, session), failed)

    assert replacement is session.added[0]
    assert replacement.document_id == document.id
    assert replacement.force is True
    assert replacement.status == JobStatus.queued
    assert failed.status == JobStatus.failed
    assert failed.diagnostic == "safe old failure"
    assert document.status == DocumentStatus.queued
    assert document.diagnostic is None


async def test_retry_keeps_a_verified_document_available() -> None:
    document = _document()
    document.active_index_version_id = uuid.uuid4()
    document.status = DocumentStatus.ready
    failed = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status=JobStatus.failed,
    )

    await retry_failed_ingestion(cast(Any, _RetrySession(document)), failed)

    assert document.status == DocumentStatus.ready
    assert document.active_index_version_id is not None


async def test_document_retry_uses_only_the_latest_ingestion_job() -> None:
    document = _document()
    latest = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status=JobStatus.failed,
    )
    session = _DocumentRetrySession(document, latest)

    replacement = await retry_latest_document_ingestion(cast(Any, session), document.id)

    assert replacement.document_id == document.id
    assert replacement.force is True
    assert session.scalar_calls == 2

    completed = IngestionJob(document_id=document.id, status=JobStatus.completed)
    with pytest.raises(IngestionRetryConflict, match="only failed"):
        await retry_latest_document_ingestion(
            cast(Any, _DocumentRetrySession(document, completed)), document.id
        )

    with pytest.raises(IngestionRetryConflict, match="no ingestion work"):
        await retry_latest_document_ingestion(
            cast(Any, _DocumentRetrySession(document, None)), document.id
        )


@pytest.mark.parametrize(
    "mode",
    [IngestionMode.rebuild_index, IngestionMode.reextract_all],
)
async def test_mode_only_rebuilds_bypass_document_deduplication(mode: IngestionMode) -> None:
    document = _document()
    session = _RegisterSession(document)

    returned, job, duplicate = await register_pdf(
        cast(Any, session),
        cast(Any, _Store()),
        b"%PDF-existing",
        "paper.pdf",
        Settings(),
        force=False,
        mode=mode,
    )

    assert returned is document
    assert duplicate is True
    assert job is session.added[0]
    assert isinstance(job, IngestionJob)
    assert job.force is True
    assert job.mode == mode
    assert document.status == DocumentStatus.queued


@pytest.mark.parametrize(
    "mode",
    [IngestionMode.rebuild_index, IngestionMode.reextract_all],
)
async def test_mode_only_rebuild_keeps_the_active_index_retrievable(
    mode: IngestionMode,
) -> None:
    document = _document()
    document.active_index_version_id = uuid.uuid4()
    document.status = DocumentStatus.ready
    document.diagnostic = "stale failure"

    _, job, _ = await register_pdf(
        cast(Any, _RegisterSession(document)),
        cast(Any, _Store()),
        b"%PDF-existing",
        "paper.pdf",
        Settings(),
        mode=mode,
    )

    assert job is not None
    assert document.status == DocumentStatus.ready
    assert document.diagnostic is None


@pytest.mark.parametrize(
    ("mode", "reuses_metadata"),
    [
        (IngestionMode.rebuild_index, True),
        (IngestionMode.reextract_all, False),
    ],
)
async def test_prepare_claim_distinguishes_rebuild_from_reextract(
    mode: IngestionMode,
    reuses_metadata: bool,
) -> None:
    document = _document()
    job, attempt, _ = _running_claim(document, mode=mode)
    stored_metadata = DocumentMetadataVersion(
        id=uuid.uuid4(),
        document_id=document.id,
        recipe_key=metadata_recipe_key(Settings()),
        configuration_hash=metadata_recipe_key(Settings()),
        content_sha256="c" * 64,
        metadata_payload={
            "title": "Stored paper",
            "authors": [],
            "abstract": "Stored abstract",
            "topics": [],
            "description": "Stored description",
            "submitted_date": None,
        },
        page_count=4,
        extraction_quality=0.9,
    )
    session = _ClaimSession(
        {
            IngestionJob: job,
            IngestionAttempt: attempt,
            Document: document,
        },
        scalar_values=[stored_metadata] if reuses_metadata else [],
    )
    processor = IngestionProcessor(
        Settings(),
        cast(Any, _Store()),
        cast(Any, object()),
        cast(Any, _V2PageIndex()),
    )

    prepared = await processor.prepare_claim(
        cast(Any, session),
        job_id=job.id,
        attempt_id=attempt.id,
        worker_id="test-worker",
        lease_token=cast(uuid.UUID, job.lease_token),
    )

    assert (prepared.metadata_version_id == stored_metadata.id) is reuses_metadata
    assert (prepared.metadata is not None) is reuses_metadata
    assert prepared.reusable_index_version_id is None


async def test_fresh_metadata_extraction_has_the_ingestion_usage_owner(
    monkeypatch: MonkeyPatch,
) -> None:
    document = _document()
    _, attempt, prepared = _running_claim(document, mode=IngestionMode.reextract_all)
    observed_owner: UsageOwner | None = None

    async def extract(
        data: bytes,
        filename: str,
        llm: object,
        settings: Settings,
    ) -> tuple[DocumentMetadata, int, float]:
        nonlocal observed_owner
        del data, filename, llm, settings
        observed_owner = current_usage_owner()
        return (
            DocumentMetadata(
                title="Fresh paper",
                authors=[],
                abstract="Abstract",
                topics=[],
                description="Description",
            ),
            1,
            1.0,
        )

    async def skip_store(*args: object) -> None:
        del args

    async def skip_load(*args: object) -> None:
        del args

    async def inline_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("vectorless_rag.ingestion.extract_metadata", extract)
    monkeypatch.setattr("vectorless_rag.ingestion.store_build_v2", skip_store)
    monkeypatch.setattr("vectorless_rag.ingestion.load_manifest_v2", skip_load)
    monkeypatch.setattr("vectorless_rag.ingestion.asyncio.to_thread", inline_to_thread)
    pageindex = _V2PageIndex()
    processor = IngestionProcessor(
        Settings(),
        cast(Any, _Store()),
        cast(Any, object()),
        cast(Any, pageindex),
    )

    build = await processor.build_claim(prepared)

    assert build is not None
    assert build.metadata_version_id is None
    assert observed_owner == UsageOwner(
        trace_id=attempt.trace_id,
        ingestion_attempt_id=attempt.id,
    )
    assert pageindex.owner == (attempt.id, attempt.trace_id)


async def test_rebuild_uses_prepared_metadata_without_model_extraction(
    monkeypatch: MonkeyPatch,
) -> None:
    document = _document()
    _, _, prepared = _running_claim(document)
    metadata_version_id = uuid.uuid4()
    metadata = DocumentMetadata(
        title="Stored paper",
        authors=[],
        abstract="Stored abstract",
        topics=[],
        description="Stored description",
    )
    prepared = replace(
        prepared,
        metadata_version_id=metadata_version_id,
        metadata=metadata,
        metadata_page_count=7,
        metadata_extraction_quality=0.8,
    )

    async def fail_extract(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("rebuild_index must not re-extract compatible metadata")

    async def skip_store(*args: object) -> None:
        del args

    async def skip_load(*args: object) -> None:
        del args

    async def inline_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("vectorless_rag.ingestion.extract_metadata", fail_extract)
    monkeypatch.setattr("vectorless_rag.ingestion.store_build_v2", skip_store)
    monkeypatch.setattr("vectorless_rag.ingestion.load_manifest_v2", skip_load)
    monkeypatch.setattr("vectorless_rag.ingestion.asyncio.to_thread", inline_to_thread)
    pageindex = _V2PageIndex()
    processor = IngestionProcessor(
        Settings(),
        cast(Any, _Store()),
        cast(Any, object()),
        cast(Any, pageindex),
    )

    build = await processor.build_claim(prepared)

    assert build is not None
    assert build.metadata_version_id == metadata_version_id
    assert build.page_count == 7
    assert build.extraction_quality == 0.8
    assert pageindex.metadata == metadata.model_dump(mode="json")


async def test_provider_failures_persist_only_stable_safe_metadata() -> None:
    document = _document()
    job, attempt, prepared = _running_claim(document)
    session = _ClaimSession(
        {
            IngestionJob: job,
            IngestionAttempt: attempt,
            Document: document,
        }
    )
    processor = IngestionProcessor(
        Settings(),
        cast(Any, _Store()),
        cast(Any, object()),
        cast(Any, _V2PageIndex()),
    )

    await processor.fail_claim(
        cast(Any, session),
        prepared,
        PageIndexError(
            "pageindex_content_filter",
            "The provider rejected PageIndex content under its content policy.",
            attempts=1,
            finish_reason="content_filter",
        ),
    )

    assert job.status == JobStatus.failed
    assert job.metrics["failure_code"] == "pageindex_content_filter"
    assert job.metrics["provider_finish_reason"] == "content_filter"
    assert job.metrics["llm_attempts"] == 1
    assert document.diagnostic == (
        "The provider rejected PageIndex content under its content policy."
    )
    assert "private-document" not in repr((job.diagnostic, job.metrics, document.diagnostic))


@pytest.mark.parametrize(
    ("failure_code", "retry_consumed"),
    [
        ("pageindex_provider_unavailable", False),
        ("pageindex_authentication_failed", False),
        ("pageindex_permission_denied", False),
        ("pageindex_transient_provider_error", True),
        ("pageindex_rate_limited", True),
        ("pageindex_network_error", True),
        ("pageindex_insufficient_system_resource", True),
        ("pageindex_llm_retry_exhausted", True),
        ("pageindex_provider_server_error", True),
    ],
)
async def test_pageindex_provider_failures_enter_bounded_waiting_state(
    failure_code: str,
    retry_consumed: bool,
) -> None:
    document = _document()
    job, attempt, prepared = _running_claim(document)
    session = _ClaimSession(
        {
            IngestionJob: job,
            IngestionAttempt: attempt,
            Document: document,
        }
    )
    processor = IngestionProcessor(
        Settings(),
        cast(Any, _Store()),
        cast(Any, object()),
        cast(Any, _V2PageIndex()),
    )

    await processor.fail_claim(
        cast(Any, session),
        prepared,
        PageIndexError(failure_code, "Safe provider interruption.", attempts=3),
    )

    assert job.status == JobStatus.waiting_provider
    assert job.is_active is True
    assert job.provider_wait_reason == failure_code
    assert attempt.status == IngestionAttemptStatus.deferred
    assert attempt.retry_budget_consumed is retry_consumed
    assert job.retry_budget_consumed == int(retry_consumed)
    assert document.status == DocumentStatus.waiting_provider
    provider_state = next(value for value in session.added if isinstance(value, ProviderState))
    assert provider_state.availability == ProviderAvailability.unavailable
    assert provider_state.reason == failure_code


async def test_experiment_cost_cap_remains_inactive_and_deferred() -> None:
    document = _document()
    job, attempt, prepared = _running_claim(document)
    session = _ClaimSession(
        {
            IngestionJob: job,
            IngestionAttempt: attempt,
            Document: document,
        }
    )
    processor = IngestionProcessor(
        Settings(),
        cast(Any, _Store()),
        cast(Any, object()),
        cast(Any, _V2PageIndex()),
    )

    await processor.fail_claim(cast(Any, session), prepared, ExperimentBudgetExceeded())

    assert job.status == JobStatus.deferred
    assert job.is_active is False
    assert job.retry_budget_consumed == 0
    assert attempt.status == IngestionAttemptStatus.deferred
    assert attempt.retry_budget_consumed is False
    assert job.metrics["failure_code"] == "experiment_cost_cap"
    assert document.status == DocumentStatus.deferred
