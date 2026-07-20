import uuid
from typing import Any, Protocol, cast

import fitz
import pytest
from pytest import MonkeyPatch

from vectorless_rag.config import Settings
from vectorless_rag.ingestion import (
    IngestionProcessor,
    IngestionRetryConflict,
    UnsupportedPDF,
    arxiv_id_from_filename,
    extract_first_pages,
    normalize_document_title,
    retry_failed_ingestion,
    sha256_bytes,
    validate_pdf,
)
from vectorless_rag.models import Document, DocumentStatus, IngestionJob, JobStatus
from vectorless_rag.pageindex_adapter import PageIndexError
from vectorless_rag.schemas import DocumentMetadata


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


class _ProcessingSession:
    def __init__(self, document: Document) -> None:
        self.document = document

    async def get(
        self, model: object, identifier: object, *, with_for_update: bool = False
    ) -> Document:
        del model, identifier, with_for_update
        return self.document

    async def scalar(self, statement: object) -> None:
        del statement
        return None

    async def flush(self) -> None:
        return None


class _Store:
    async def get_bytes(self, key: str) -> bytes:
        del key
        return b"%PDF-private-document"


class _FailingPageIndex:
    async def index(self, path: object, document_id: str, metadata: object) -> bytes:
        del path, document_id, metadata
        raise PageIndexError(
            "pageindex_content_filter",
            "The provider rejected PageIndex content under its content policy.",
            attempts=1,
            finish_reason="content_filter",
        )


def _document() -> Document:
    return Document(
        id=uuid.uuid4(),
        sha256="a" * 64,
        original_filename="paper.pdf",
        pdf_object_key="pdf/paper.pdf",
        status=DocumentStatus.failed,
    )


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
    with pytest.raises(UnsupportedPDF, match="only .pdf"):
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


async def test_provider_failures_persist_only_stable_safe_metadata(
    monkeypatch: MonkeyPatch,
) -> None:
    document = _document()
    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status=JobStatus.running,
        metrics={},
    )

    async def metadata(*args: object, **kwargs: object) -> tuple[DocumentMetadata, int, float]:
        del args, kwargs
        return (
            DocumentMetadata(
                title="Paper",
                authors=[],
                abstract="Abstract",
                topics=[],
                description="Description",
            ),
            2,
            1.0,
        )

    async def inline_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("vectorless_rag.ingestion.extract_metadata", metadata)
    monkeypatch.setattr("vectorless_rag.ingestion.asyncio.to_thread", inline_to_thread)
    processor = IngestionProcessor(
        Settings(),
        cast(Any, _Store()),
        cast(Any, object()),
        cast(Any, _FailingPageIndex()),
    )

    with pytest.raises(PageIndexError):
        await processor.process(cast(Any, _ProcessingSession(document)), job)

    assert job.status == JobStatus.failed
    assert job.metrics["failure_code"] == "pageindex_content_filter"
    assert job.metrics["provider_finish_reason"] == "content_filter"
    assert job.metrics["llm_attempts"] == 1
    assert document.diagnostic == (
        "The provider rejected PageIndex content under its content policy."
    )
    assert "private-document" not in repr((job.diagnostic, job.metrics, document.diagnostic))
