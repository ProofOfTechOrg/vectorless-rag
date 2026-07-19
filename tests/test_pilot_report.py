from vectorless_rag.ingestion import build_pilot_report
from vectorless_rag.models import IngestionJob, JobStatus


def test_pilot_report_never_implies_full_corpus_approval() -> None:
    job = IngestionJob(
        status=JobStatus.completed,
        document_id="00000000-0000-0000-0000-000000000001",
        metrics={"elapsed_seconds": 2, "extraction_quality": 0.9, "input_tokens": 10},
    )
    report = build_pilot_report(
        [job], input_cost_per_million_usd=1.0, output_cost_per_million_usd=2.0
    )
    assert report["completed"] == 1
    assert report["full_corpus_approved"] is False
    assert report["projected_full_corpus_input_tokens"] == 5370
    assert report["projected_full_corpus_spend_usd"] == 0.01
    assert "ALLOW_FULL_CORPUS_INDEX" in str(report["note"])
