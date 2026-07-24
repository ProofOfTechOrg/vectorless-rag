import uuid
from typing import Any, cast

import pytest

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


def _attempt_call(
    attempt: IngestionAttempt,
    *,
    outcome: ModelCallOutcome = ModelCallOutcome.completed,
    stage: ModelCallStage = ModelCallStage.pageindex_generation,
    usage_available: bool = True,
) -> ModelCall:
    usage = (
        {
            "input_tokens": 5,
            "cache_hit_input_tokens": 0,
            "cache_miss_input_tokens": 5,
            "cache_write_input_tokens": 0,
            "unattributed_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
            "total_tokens": 6,
        }
        if usage_available
        else {}
    )
    return ModelCall(
        id=uuid.uuid4(),
        ingestion_attempt_id=attempt.id,
        trace_id=attempt.trace_id,
        stage=stage,
        model="provider/model",
        provider_attempt=1,
        outcome=outcome,
        usage_available=usage_available,
        cache_attribution_complete=usage_available,
        **usage,
    )


def _retry_attempt(
    job: IngestionJob,
    *,
    telemetry_complete: bool,
) -> IngestionAttempt:
    return IngestionAttempt(
        id=uuid.uuid4(),
        job_id=job.id,
        attempt_number=2,
        trace_id=uuid.uuid4().hex,
        worker_id="worker-1",
        status=IngestionAttemptStatus.completed,
        artifact_action=ArtifactAction.reused,
        page_count=10,
        telemetry_complete=telemetry_complete,
    )


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


def test_metadata_and_pageindex_projections_use_independent_scaling_factors() -> None:
    document, job, attempt, pageindex_call = _cohort()
    metadata_call = ModelCall(
        id=uuid.uuid4(),
        ingestion_attempt_id=attempt.id,
        trace_id=attempt.trace_id,
        stage=ModelCallStage.ingestion_metadata,
        model="provider/model",
        provider_attempt=1,
        outcome=ModelCallOutcome.completed,
        input_tokens=10,
        cache_hit_input_tokens=0,
        cache_miss_input_tokens=10,
        cache_write_input_tokens=0,
        unattributed_input_tokens=0,
        output_tokens=2,
        reasoning_output_tokens=0,
        total_tokens=12,
        usage_available=True,
        cache_attribution_complete=True,
    )

    report = cast(
        dict[str, Any],
        build_pilot_report(
            [document],
            [job],
            [attempt],
            [metadata_call, pageindex_call],
            CorpusMeasurement(4, 100, (20, 20, 30, 30), 0),
            Pricing(2.0, 1.0, 3.0),
        ),
    )

    projection = report["projection"]["successful_index_page_ratio"]
    metadata = projection["by_stage"]["metadata"]
    pageindex = projection["by_stage"]["pageindex"]
    combined = projection["by_stage"]["combined"]
    assert projection["sample_documents"] == 1
    assert projection["corpus_documents"] == 4
    assert projection["document_ratio"] == 4.0
    assert projection["page_ratio"] == 10.0
    assert metadata["projected_usage"]["input_tokens"] == 40
    assert metadata["projected_usage"]["output_tokens"] == 8
    assert pageindex["projected_usage"]["input_tokens"] == 100
    assert pageindex["projected_usage"]["output_tokens"] == 40
    assert combined["projected_usage"]["input_tokens"] == 140
    assert combined["projected_usage"]["output_tokens"] == 48
    assert combined["projected_cost"]["known_cost_usd"] == pytest.approx(
        metadata["projected_cost"]["known_cost_usd"] + pageindex["projected_cost"]["known_cost_usd"]
    )


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


@pytest.mark.parametrize(
    ("outcome", "usage_available"),
    [
        (ModelCallOutcome.started, True),
        (ModelCallOutcome.completed, False),
    ],
)
def test_incomplete_retry_calls_disable_retry_adjustment(
    outcome: ModelCallOutcome,
    usage_available: bool,
) -> None:
    document, job, sample_attempt, sample_call = _cohort()
    retry_attempt = _retry_attempt(job, telemetry_complete=True)
    retry_call = _attempt_call(
        retry_attempt,
        outcome=outcome,
        usage_available=usage_available,
    )

    report = cast(
        dict[str, Any],
        build_pilot_report(
            [document],
            [job],
            [sample_attempt, retry_attempt],
            [sample_call, retry_call],
            CorpusMeasurement(1, 10, (10,), 0),
            Pricing(2.0, 1.0, 3.0),
        ),
    )

    retry_adjusted = report["projection"]["retry_adjusted"]
    assert retry_adjusted["eligible"] is False
    assert retry_adjusted["reason"] == "retry_telemetry_incomplete"


def test_incomplete_non_sample_attempt_disables_retry_adjustment() -> None:
    document, job, sample_attempt, sample_call = _cohort()
    retry_attempt = _retry_attempt(job, telemetry_complete=False)

    report = cast(
        dict[str, Any],
        build_pilot_report(
            [document],
            [job],
            [sample_attempt, retry_attempt],
            [sample_call],
            CorpusMeasurement(1, 10, (10,), 0),
            Pricing(2.0, 1.0, 3.0),
        ),
    )

    retry_adjusted = report["projection"]["retry_adjusted"]
    assert retry_adjusted["eligible"] is False
    assert retry_adjusted["reason"] == "retry_telemetry_incomplete"


def test_legacy_and_current_jobs_disable_retry_adjustment() -> None:
    document, job, sample_attempt, sample_call = _cohort()
    legacy_document = Document(
        id=uuid.uuid4(),
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_filename="legacy.pdf",
        pdf_object_key="pdf/legacy",
        status=DocumentStatus.ready,
        page_count=10,
    )
    legacy_job = IngestionJob(
        id=uuid.uuid4(),
        document_id=legacy_document.id,
        status=JobStatus.completed,
        attempts=1,
    )

    report = cast(
        dict[str, Any],
        build_pilot_report(
            [document, legacy_document],
            [job, legacy_job],
            [sample_attempt],
            [sample_call],
            CorpusMeasurement(2, 20, (10, 10), 0),
            Pricing(2.0, 1.0, 3.0),
        ),
    )

    retry_adjusted = report["projection"]["retry_adjusted"]
    assert retry_adjusted["eligible"] is False
    assert retry_adjusted["reason"] == "retry_telemetry_incomplete"


def test_fully_measured_retries_remain_eligible_for_adjustment() -> None:
    document, job, sample_attempt, sample_call = _cohort()
    retry_attempt = _retry_attempt(job, telemetry_complete=True)
    retry_call = _attempt_call(retry_attempt)

    report = cast(
        dict[str, Any],
        build_pilot_report(
            [document],
            [job],
            [sample_attempt, retry_attempt],
            [sample_call, retry_call],
            CorpusMeasurement(1, 10, (10,), 0),
            Pricing(2.0, 1.0, 3.0),
        ),
    )

    retry_adjusted = report["projection"]["retry_adjusted"]
    assert retry_adjusted["eligible"] is True
    assert retry_adjusted["observed_overhead_factor"] == pytest.approx(20 / 14)


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
