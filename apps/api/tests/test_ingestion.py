from typing import Protocol, cast

import fitz
import pytest

from vectorless_rag.ingestion import (
    UnsupportedPDF,
    arxiv_id_from_filename,
    extract_first_pages,
    normalize_document_title,
    sha256_bytes,
    validate_pdf,
)


class _WritablePage(Protocol):
    def insert_text(self, point: tuple[int, int], text: str) -> int: ...


class _WritableDocument(Protocol):
    def new_page(self) -> _WritablePage: ...

    def set_metadata(self, metadata: dict[str, str]) -> None: ...

    def tobytes(self) -> bytes: ...

    def close(self) -> None: ...


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
