import pytest

from vectorless_rag.ingestion import (
    UnsupportedPDF,
    arxiv_id_from_filename,
    sha256_bytes,
    validate_pdf,
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
