from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from vectorless_rag.models import (
    ModelCall,
    ModelCallOutcome,
    ModelCallStage,
    QueryRun,
    QueryRunStatus,
    RunFailureCode,
    RunRoute,
    query_run_status,
)
from vectorless_rag.operational_logging import emit_event, event_json
from vectorless_rag.reporting import Pricing, build_query_cost_report
from vectorless_rag.usage import (
    UsageOwner,
    current_usage_owner,
    normalize_token_usage,
    usage_context,
)


class _FailingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        del record
        raise RuntimeError("handler failed")


def _query_run(
    *,
    answer: str | None = None,
    error: str | None = None,
    failure_code: RunFailureCode | None = None,
    question: str = "What is indexed?",
    route: RunRoute | None = None,
) -> QueryRun:
    return QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id=uuid.uuid4().hex,
        api_key_id=uuid.uuid4(),
        route=route,
        question=question,
        answer=answer,
        error=error,
        failure_code=failure_code,
    )


def _query_call(
    run: QueryRun,
    *,
    outcome: ModelCallOutcome,
    usage_available: bool,
) -> ModelCall:
    usage = (
        {
            "input_tokens": 0,
            "cache_hit_input_tokens": 0,
            "cache_miss_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "unattributed_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }
        if usage_available
        else {}
    )
    return ModelCall(
        id=uuid.uuid4(),
        query_run_id=run.id,
        trace_id=run.trace_id,
        stage=ModelCallStage.query_route,
        model="provider/model",
        provider_attempt=1,
        outcome=outcome,
        usage_available=usage_available,
        cache_attribution_complete=usage_available,
        **usage,
    )


def test_langchain_usage_preserves_cache_and_reasoning_partitions() -> None:
    usage = normalize_token_usage(
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "total_tokens": 999,
            "input_token_details": {"cache_read": 30, "cache_creation": 20},
            "output_token_details": {"reasoning": 12},
        }
    )

    assert usage.usage_available
    assert usage.cache_attribution_complete
    assert usage.input_tokens == 100
    assert usage.cache_hit_input_tokens == 30
    assert usage.cache_miss_input_tokens == 70
    assert usage.cache_write_input_tokens == 20
    assert usage.unattributed_input_tokens == 0
    assert usage.output_tokens == 40
    assert usage.reasoning_output_tokens == 12
    assert usage.total_tokens == 140


def test_litellm_usage_and_missing_cache_details_are_normalized_honestly() -> None:
    litellm_usage = SimpleNamespace(
        prompt_tokens=80,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=25),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
    )
    normalized = normalize_token_usage(litellm_usage)
    unattributed = normalize_token_usage({"prompt_tokens": 9, "completion_tokens": 1})

    assert normalized.cache_hit_input_tokens == 25
    assert normalized.cache_miss_input_tokens == 55
    assert normalized.reasoning_output_tokens == 7
    assert normalized.cache_attribution_complete
    assert unattributed.unattributed_input_tokens == 9
    assert unattributed.cache_miss_input_tokens == 0
    assert not unattributed.cache_attribution_complete


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"input_tokens": -1, "output_tokens": 2},
        {"input_tokens": True, "output_tokens": 2},
        {"total_tokens": 4},
    ],
)
def test_invalid_or_incomplete_usage_is_unavailable(raw: object) -> None:
    usage = normalize_token_usage(raw)

    assert not usage.usage_available
    assert usage.input_tokens is None
    assert usage.total_tokens is None


def test_invalid_cache_and_reasoning_details_cannot_break_partitions() -> None:
    usage = normalize_token_usage(
        {
            "input_tokens": 5,
            "output_tokens": 2,
            "input_token_details": {"cache_read": 6, "cache_creation": 8},
            "output_token_details": {"reasoning": 3},
        }
    )

    assert usage.cache_hit_input_tokens == 0
    assert usage.cache_miss_input_tokens == 0
    assert usage.cache_write_input_tokens == 0
    assert usage.unattributed_input_tokens == 5
    assert usage.reasoning_output_tokens == 0
    assert usage.total_tokens == 7


async def test_concurrent_usage_contexts_keep_attempt_and_trace_ownership() -> None:
    async def observe(attempt_id: uuid.UUID, trace_id: str) -> UsageOwner | None:
        with usage_context(UsageOwner(trace_id=trace_id, ingestion_attempt_id=attempt_id)):
            await asyncio.sleep(0)
            return current_usage_owner()

    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    first_trace = uuid.uuid4().hex
    second_trace = uuid.uuid4().hex
    first, second = await asyncio.gather(
        observe(first_id, first_trace),
        observe(second_id, second_trace),
    )

    assert first == UsageOwner(trace_id=first_trace, ingestion_attempt_id=first_id)
    assert second == UsageOwner(trace_id=second_trace, ingestion_attempt_id=second_id)
    assert current_usage_owner() is None


def test_operational_events_are_one_line_allowlisted_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("usage-event-test")
    with caplog.at_level(logging.INFO, logger=logger.name):
        emit_event(
            logger,
            "query_failed",
            trace_id=uuid.uuid4().hex,
            status="failed",
            failure_code="request_failed",
            elapsed_ms=12,
            release="test-release",
        )

    payload = json.loads(caplog.messages[-1])
    assert payload["event"] == "query_failed"
    assert "\n" not in caplog.messages[-1]
    assert set(payload) == {
        "event",
        "trace_id",
        "status",
        "failure_code",
        "elapsed_ms",
        "release",
    }
    with pytest.raises(ValueError):
        event_json("query_failed", error="private provider exception")
    with pytest.raises(ValueError):
        event_json("query_failed", trace_id="unsafe\nprompt")


def test_operational_event_emission_drops_unsafe_fields_without_logging_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("unsafe-usage-event-test")
    rejected_release = "private\nrelease"

    with caplog.at_level(logging.WARNING, logger=logger.name):
        emit_event(
            logger,
            "query_failed",
            status="failed",
            release=rejected_release,
        )

    assert caplog.messages == ["operational_event_dropped"]
    assert rejected_release not in caplog.text


def test_operational_event_emission_swallows_log_handler_failures() -> None:
    logger = logging.getLogger("failing-usage-event-test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _FailingHandler()
    logger.addHandler(handler)
    try:
        emit_event(logger, "query_started", status="running", release="test")
    finally:
        logger.removeHandler(handler)
        logger.propagate = True


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_query_run(), QueryRunStatus.running),
        (_query_run(answer="answer"), QueryRunStatus.completed),
        (
            _query_run(failure_code=RunFailureCode.request_failed),
            QueryRunStatus.failed,
        ),
        (_query_run(error="request failed"), QueryRunStatus.failed),
    ],
)
def test_query_run_status_classifies_durable_and_legacy_states(
    run: QueryRun,
    expected: QueryRunStatus,
) -> None:
    assert query_run_status(run) == expected


def test_query_cost_report_is_aggregate_cache_aware_and_content_free() -> None:
    run_id = uuid.uuid4()
    trace_id = uuid.uuid4().hex
    private_question = "private unique question"
    private_answer = "private unique answer"
    private_key_id = uuid.uuid4()
    run = QueryRun(
        id=run_id,
        trace_id=trace_id,
        thread_id="private-thread",
        api_key_id=private_key_id,
        route=RunRoute.documents,
        question=private_question,
        answer=private_answer,
    )
    call = ModelCall(
        id=uuid.uuid4(),
        query_run_id=run_id,
        trace_id=trace_id,
        stage=ModelCallStage.query_synthesis,
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

    report = cast(
        dict[str, Any],
        build_query_cost_report([run], [call], Pricing(2.0, 1.0, 3.0), days=30),
    )

    group = report["groups"][0]
    assert group["route"] == "documents"
    assert group["outcome"] == "completed"
    assert group["known_cost"]["known_cost_usd"] == 0.00003
    assert group["average_known_cost_per_run_usd"] == 0.00003
    serialized = repr(report)
    for forbidden in (
        private_question,
        private_answer,
        str(private_key_id),
        "private-thread",
        trace_id,
    ):
        assert forbidden not in serialized


def test_static_help_without_calls_is_known_zero_but_other_help_is_incomplete() -> None:
    exact = _query_run(
        answer="Supported tasks",
        question="What can you do?",
        route=RunRoute.help,
    )
    routed = _query_run(
        answer="A model-routed answer",
        question="Explain how you can help with this paper",
        route=RunRoute.help,
    )
    pricing = Pricing(None, None, None)

    exact_report = cast(
        dict[str, Any],
        build_query_cost_report([exact], [], pricing, days=30),
    )
    routed_report = cast(
        dict[str, Any],
        build_query_cost_report([routed], [], pricing, days=30),
    )

    exact_group = exact_report["groups"][0]
    assert exact_group["incomplete_runs"] == 0
    assert exact_group["known_cost"]["known_cost_usd"] == 0.0
    assert exact_group["known_cost"]["missing_price_configuration"]
    assert exact_group["average_known_cost_per_run_usd"] == 0.0
    assert exact_group["runs_with_known_cost"] == 1
    routed_group = routed_report["groups"][0]
    assert routed_group["incomplete_runs"] == 1
    assert routed_group["known_cost"]["known_cost_usd"] is None
    assert routed_group["runs_with_known_cost"] == 0


def test_started_usage_unavailable_call_counts_as_one_incomplete_call() -> None:
    run = _query_run(route=RunRoute.documents)
    call = _query_call(
        run,
        outcome=ModelCallOutcome.started,
        usage_available=False,
    )

    report = cast(
        dict[str, Any],
        build_query_cost_report([run], [call], Pricing(1.0, 1.0, 1.0), days=30),
    )

    assert report["totals"]["incomplete_calls"] == 1
    assert report["groups"][0]["incomplete_calls"] == 1


def test_active_run_with_completed_partial_calls_stays_running_and_incomplete() -> None:
    run = _query_run(route=RunRoute.documents)
    call = _query_call(
        run,
        outcome=ModelCallOutcome.completed,
        usage_available=True,
    )

    report = cast(
        dict[str, Any],
        build_query_cost_report([run], [call], Pricing(1.0, 1.0, 1.0), days=30),
    )

    group = report["groups"][0]
    assert group["outcome"] == "running"
    assert group["incomplete_runs"] == 1
    assert group["incomplete_calls"] == 0
    assert report["totals"]["incomplete_runs"] == 1
    assert report["totals"]["incomplete_calls"] == 0
