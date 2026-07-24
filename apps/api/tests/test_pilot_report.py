import uuid
from typing import Any, cast

from vectorless_rag.ingestion import build_pilot_report
from vectorless_rag.models import (
    ArtifactAction,
    Document,
    DocumentStatus,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    JobStatus,
    ModelCall,
    ModelCallOutcome,
    ModelCallStage,
)
from vectorless_rag.reporting import CorpusMeasurement, Pricing


def _cohort() -> tuple[
    Document,
    IngestionJob,
    IngestionAttempt,
    ModelCall,
]:
    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    trace_id = uuid.uuid4().hex
    document = Document(
        id=document_id,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_filename="private-name.pdf",
        pdf_object_key="pdf/private",
        status=DocumentStatus.ready,
        page_count=10,
    )
    job = IngestionJob(
        id=job_id,
        document_id=document_id,
        status=JobStatus.completed,
        attempts=1,
    )
    attempt = IngestionAttempt(
        id=attempt_id,
        job_id=job_id,
        attempt_number=1,
        trace_id=trace_id,
        worker_id="worker-1",
        status=IngestionAttemptStatus.completed,
        artifact_action=ArtifactAction.created,
        page_count=10,
        telemetry_complete=True,
    )
    call = ModelCall(
        id=uuid.uuid4(),
        ingestion_attempt_id=attempt_id,
        trace_id=trace_id,
        stage=ModelCallStage.pageindex_generation,
        model="provider/model",
        provider_attempt=1,
        outcome=ModelCallOutcome.completed,
        input_tokens=10,
        cache_hit_input_tokens=2,
        cache_miss_input_tokens=3,
        cache_write_input_tokens=1,
        unattributed_input_tokens=5,
        output_tokens=4,
        reasoning_output_tokens=2,
        total_tokens=14,
        usage_available=True,
        cache_attribution_complete=False,
    )
    return document, job, attempt, call


def test_pilot_report_uses_schema_two_page_projection_and_keeps_gate_blocked() -> None:
    document, job, attempt, call = _cohort()
    report = cast(
        dict[str, Any],
        build_pilot_report(
            [document],
            [job],
            [attempt],
            [call],
            CorpusMeasurement(
                direct_pdf_count=5,
                total_pages=50,
                page_counts=(2, 5, 10, 15, 18),
                unreadable_files=0,
            ),
            Pricing(2.0, 1.0, 3.0),
        ),
    )

    assert report["schema_version"] == 2
    assert report["cohort"]["documents"] == 1
    assert report["measured"]["combined"]["input_tokens"] == 10
    assert report["measured"]["combined"]["unattributed_input_tokens"] == 5
    assert report["measured"]["combined"]["cost"]["classification"] == "upper_bound"
    projection = report["projection"]["successful_index_page_ratio"]
    assert projection["projected_usage"]["input_tokens"] == 50
    assert projection["projected_usage"]["output_tokens"] == 20
    assert report["gate"] == {
        "full_corpus_approved": False,
        "approval_ready": False,
        "reason": "representative_pilot_required",
    }


def test_legacy_jobs_are_unknown_instead_of_zero_measured_usage() -> None:
    document, job, _, _ = _cohort()
    report = cast(
        dict[str, Any],
        build_pilot_report(
            [document],
            [job],
            [],
            [],
            CorpusMeasurement(1, 10, (10,), 0),
            Pricing(None, None, None),
        ),
    )

    assert report["attempts"]["legacy_jobs_without_attempts"] == 1
    assert report["measured"]["combined"]["calls"] == 0
    assert report["measured"]["combined"]["input_tokens"] is None
    assert report["projection"]["successful_index_page_ratio"]["eligible"] is False
    assert report["gate"]["full_corpus_approved"] is False


def test_regression_projection_requires_and_uses_five_distinct_page_counts() -> None:
    documents: list[Document] = []
    jobs: list[IngestionJob] = []
    attempts: list[IngestionAttempt] = []
    calls: list[ModelCall] = []
    for pages in (2, 4, 6, 8, 10):
        document_id = uuid.uuid4()
        job_id = uuid.uuid4()
        attempt_id = uuid.uuid4()
        trace_id = uuid.uuid4().hex
        documents.append(
            Document(
                id=document_id,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                original_filename=f"private-{pages}.pdf",
                pdf_object_key=f"pdf/{pages}",
                status=DocumentStatus.ready,
                page_count=pages,
            )
        )
        jobs.append(
            IngestionJob(
                id=job_id,
                document_id=document_id,
                status=JobStatus.completed,
                attempts=1,
            )
        )
        attempts.append(
            IngestionAttempt(
                id=attempt_id,
                job_id=job_id,
                attempt_number=1,
                trace_id=trace_id,
                worker_id="worker-1",
                status=IngestionAttemptStatus.completed,
                artifact_action=ArtifactAction.created,
                page_count=pages,
                telemetry_complete=True,
            )
        )
        calls.append(
            ModelCall(
                id=uuid.uuid4(),
                ingestion_attempt_id=attempt_id,
                trace_id=trace_id,
                stage=ModelCallStage.pageindex_generation,
                model="provider/model",
                provider_attempt=1,
                outcome=ModelCallOutcome.completed,
                input_tokens=pages * 2,
                cache_hit_input_tokens=0,
                cache_miss_input_tokens=pages * 2,
                cache_write_input_tokens=0,
                unattributed_input_tokens=0,
                output_tokens=pages,
                reasoning_output_tokens=0,
                total_tokens=pages * 3,
                usage_available=True,
                cache_attribution_complete=True,
            )
        )

    report = cast(
        dict[str, Any],
        build_pilot_report(
            documents,
            jobs,
            attempts,
            calls,
            CorpusMeasurement(5, 30, (2, 4, 6, 8, 10), 0),
            Pricing(2.0, 1.0, 3.0),
        ),
    )

    regression = report["projection"]["linear_regression_sensitivity"]
    assert regression["eligible"] is True
    assert regression["distinct_page_counts"] == 5
    assert regression["r_squared_total_tokens"] == 1.0
    assert regression["projected_usage"]["input_tokens"] == 60
    assert report["projection"]["missing_page_bands"] == []
