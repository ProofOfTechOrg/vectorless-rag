from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import fitz

from vectorless_rag.config import Settings
from vectorless_rag.graph import is_static_capability_request
from vectorless_rag.models import (
    ArtifactAction,
    Document,
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    ModelCall,
    ModelCallOutcome,
    ModelCallStage,
    QueryRun,
    QueryRunStatus,
    RunFailureCode,
    RunRoute,
    query_run_status,
)

TOKEN_FIELDS = (
    "input_tokens",
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "cache_write_input_tokens",
    "unattributed_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
ATOMIC_TOKEN_FIELDS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "cache_write_input_tokens",
    "unattributed_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


@dataclass(frozen=True)
class Pricing:
    """Rates for one model, optionally overridden per model name.

    Indexing and query serving run on different models at different rates, and every call
    records which model produced it, so a report that applies one rate to both silently
    misstates whichever model it did not describe.
    """

    cache_miss_input_per_million_usd: float | None
    cache_hit_input_per_million_usd: float | None
    output_per_million_usd: float | None
    environment_prefix: str = "DEEPSEEK"
    # The model these base rates describe. None means they describe whatever they are
    # applied to, which is only correct when the caller knows there is one model.
    model: str | None = None
    overrides: Mapping[str, Pricing] = dataclass_field(
        default_factory=lambda: cast(dict[str, "Pricing"], {})
    )

    def for_model(self, model: str) -> Pricing | None:
        """Rates for a recorded model name, or None when none are configured for it.

        Returning the base rates for an unrecognised model would price it confidently and
        wrongly: a call recorded under a model the configuration has since moved off would
        silently take the current model's rate. A caller that leaves `model` unset keeps the
        single-rate behaviour, since then the base rates describe every call by definition.
        """
        override = self.overrides.get(model)
        if override is not None:
            return override
        return self if self.model is None or self.model == model else None

    def missing(self) -> list[str]:
        return [
            f"{self.environment_prefix}_{name}_COST_PER_MILLION_USD"
            for name, value in (
                ("INPUT", self.cache_miss_input_per_million_usd),
                ("CACHE_HIT_INPUT", self.cache_hit_input_per_million_usd),
                ("OUTPUT", self.output_per_million_usd),
            )
            if value is None
        ]


def _indexing_rates(settings: Settings) -> Pricing:
    return Pricing(
        settings.pageindex_input_cost_per_million_usd,
        settings.pageindex_cache_hit_input_cost_per_million_usd,
        settings.pageindex_output_cost_per_million_usd,
        environment_prefix="PAGEINDEX",
    )


def _indexing_model_names(settings: Settings) -> tuple[str, ...]:
    # Ingestion records the model with any litellm/ prefix already stripped, so that is the
    # form calls are keyed by; the configured spelling is kept too for a report run against
    # rows written before the stripping.
    return tuple({settings.pageindex_model, settings.pageindex_model.removeprefix("litellm/")})


def _serving_rates(settings: Settings) -> Pricing:
    return Pricing(
        settings.deepseek_input_cost_per_million_usd,
        settings.deepseek_cache_hit_input_cost_per_million_usd,
        settings.deepseek_output_cost_per_million_usd,
        model=settings.deepseek_model,
    )


def query_pricing(settings: Settings) -> Pricing:
    """Rates for serving, with indexing calls priced correctly if any appear."""
    indexing = _indexing_rates(settings)
    serving = _serving_rates(settings)
    return dataclass_replace(
        serving, overrides={name: indexing for name in _indexing_model_names(settings)}
    )


def indexing_pricing(settings: Settings) -> Pricing:
    """Rates for ingestion, with serving calls priced correctly if any appear."""
    indexing = _indexing_rates(settings)
    return dataclass_replace(
        indexing,
        model=settings.pageindex_model,
        overrides={
            **{name: indexing for name in _indexing_model_names(settings)},
            settings.deepseek_model: _serving_rates(settings),
        },
    )


@dataclass(frozen=True)
class CorpusMeasurement:
    direct_pdf_count: int
    total_pages: int
    page_counts: tuple[int, ...]
    unreadable_files: int


def scan_corpus(corpus: Path) -> CorpusMeasurement:
    page_counts: list[int] = []
    unreadable = 0
    pdfs = sorted(
        path for path in corpus.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"
    )
    for path in pdfs:
        try:
            with fitz.open(path) as document:
                page_counts.append(len(document))
        except Exception:
            unreadable += 1
    return CorpusMeasurement(
        direct_pdf_count=len(pdfs),
        total_pages=sum(page_counts),
        page_counts=tuple(page_counts),
        unreadable_files=unreadable,
    )


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _call_vector(call: ModelCall) -> dict[str, int]:
    return {field: int(getattr(call, field) or 0) for field in TOKEN_FIELDS}


def _call_is_incomplete(call: ModelCall) -> bool:
    return _enum_value(call.outcome) == ModelCallOutcome.started.value or not call.usage_available


def _sum_vectors(vectors: Iterable[dict[str, int]]) -> dict[str, int]:
    result = {field: 0 for field in TOKEN_FIELDS}
    for vector in vectors:
        for field in TOKEN_FIELDS:
            result[field] += vector[field]
    return result


def _scale_vector(vector: dict[str, int], factor: float) -> dict[str, int]:
    result = {field: max(0, round(vector[field] * factor)) for field in ATOMIC_TOKEN_FIELDS}
    result["cache_write_input_tokens"] = min(
        result["cache_write_input_tokens"], result["cache_miss_input_tokens"]
    )
    result["reasoning_output_tokens"] = min(
        result["reasoning_output_tokens"], result["output_tokens"]
    )
    result["input_tokens"] = (
        result["cache_hit_input_tokens"]
        + result["cache_miss_input_tokens"]
        + result["unattributed_input_tokens"]
    )
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _usage_summary(
    calls: Sequence[ModelCall], *, empty_is_zero: bool = False
) -> tuple[dict[str, object], dict[str, int]]:
    available = [call for call in calls if call.usage_available]
    vector = _sum_vectors(_call_vector(call) for call in available)
    token_payload: dict[str, int | None]
    if available:
        token_payload = dict(vector)
    elif calls:
        token_payload = {field: None for field in TOKEN_FIELDS}
    elif empty_is_zero:
        token_payload = {field: 0 for field in TOKEN_FIELDS}
    else:
        token_payload = {field: None for field in TOKEN_FIELDS}
    payload: dict[str, object] = {
        "calls": len(calls),
        "completed_calls": sum(
            _enum_value(call.outcome) == ModelCallOutcome.completed.value for call in calls
        ),
        "failed_calls": sum(
            _enum_value(call.outcome) == ModelCallOutcome.failed.value for call in calls
        ),
        "started_calls": sum(
            _enum_value(call.outcome) == ModelCallOutcome.started.value for call in calls
        ),
        "usage_available_calls": len(available),
        "usage_unavailable_calls": len(calls) - len(available),
        "cache_attribution_incomplete_calls": sum(
            call.usage_available and not call.cache_attribution_complete for call in calls
        ),
        **token_payload,
    }
    return payload, vector


def _payload_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} is not an integer")
    return value


def _rate_cost(vector: Mapping[str, int], rates: Pricing) -> float:
    assert rates.cache_miss_input_per_million_usd is not None
    assert rates.cache_hit_input_per_million_usd is not None
    assert rates.output_per_million_usd is not None
    return (
        vector["cache_hit_input_tokens"] * rates.cache_hit_input_per_million_usd
        + (vector["cache_miss_input_tokens"] + vector["unattributed_input_tokens"])
        * rates.cache_miss_input_per_million_usd
        + vector["output_tokens"] * rates.output_per_million_usd
    ) / 1_000_000


def _price_usage(
    payload: Mapping[str, object],
    vector: dict[str, int],
    pricing: Pricing,
    calls: Sequence[ModelCall] = (),
) -> dict[str, object]:
    # Price each model's calls at its own rates; without overrides this is one group and
    # reduces exactly to the single-rate behaviour.
    grouped: defaultdict[str, list[ModelCall]] = defaultdict(list)
    for call in calls:
        grouped[call.model].append(call)
    resolved = {model: pricing.for_model(model) for model in grouped}
    rates_used = [rates for rates in resolved.values() if rates is not None] or [pricing]
    # A model with no configured rate is named in the report rather than absorbed into
    # whichever rate happened to be the base, which would be a confident wrong number.
    missing = sorted(
        {name for rates in rates_used for name in rates.missing()}
        | {f"PRICE_FOR_MODEL:{model}" for model, rates in resolved.items() if rates is None}
    )

    cost: float | None = None
    if all(payload.get(field) == 0 for field in TOKEN_FIELDS):
        cost = 0.0
    elif not missing and payload["usage_available_calls"]:
        if grouped:
            cost = sum(
                _rate_cost(_usage_summary(group)[1], rates)
                for model, group in grouped.items()
                if (rates := resolved[model]) is not None
            )
        else:
            cost = _rate_cost(vector, pricing)
    return {
        "known_cost_usd": round(cost, 6) if cost is not None else None,
        "missing_price_configuration": missing,
        "classification": (
            "unknown"
            if payload.get("input_tokens", object()) is None
            else ("upper_bound" if vector["unattributed_input_tokens"] > 0 else "cache_attributed")
        ),
        "unattributed_input_priced_as_cache_miss": True,
    }


def _usage_and_cost(calls: Sequence[ModelCall], pricing: Pricing) -> dict[str, object]:
    payload, vector = _usage_summary(calls)
    return {**payload, "cost": _price_usage(payload, vector, pricing, calls)}


def _percentile(values: Sequence[int], fraction: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _band(page_count: int, thresholds: dict[str, float]) -> str:
    if page_count <= thresholds["p25"]:
        return "lte_p25"
    if page_count <= thresholds["p50"]:
        return "p25_to_p50"
    if page_count <= thresholds["p75"]:
        return "p50_to_p75"
    if page_count <= thresholds["p90"]:
        return "p75_to_p90"
    return "gt_p90"


def _band_coverage(
    corpus_pages: Sequence[int], sample_pages: Sequence[int]
) -> tuple[dict[str, object], list[str]]:
    names = ("lte_p25", "p25_to_p50", "p50_to_p75", "p75_to_p90", "gt_p90")
    thresholds = {
        "p25": _percentile(corpus_pages, 0.25),
        "p50": _percentile(corpus_pages, 0.50),
        "p75": _percentile(corpus_pages, 0.75),
        "p90": _percentile(corpus_pages, 0.90),
    }
    corpus_counts = Counter(_band(value, thresholds) for value in corpus_pages)
    sample_counts = Counter(_band(value, thresholds) for value in sample_pages)
    coverage = {
        name: {
            "corpus_documents": corpus_counts[name],
            "sample_documents": sample_counts[name],
            "represented": sample_counts[name] > 0,
        }
        for name in names
    }
    return {
        "threshold_pages": {key: round(value, 3) for key, value in thresholds.items()},
        "bands": coverage,
    }, [name for name in names if corpus_counts[name] and not sample_counts[name]]


def _linear_fit(points: Sequence[tuple[int, int]]) -> tuple[float, float, float]:
    count = len(points)
    x_mean = sum(x for x, _ in points) / count
    y_mean = sum(y for _, y in points) / count
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    slope = (
        sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator if denominator else 0.0
    )
    intercept = y_mean - slope * x_mean
    residual = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
    total = sum((y - y_mean) ** 2 for _, y in points)
    r_squared = 1.0 if total == 0 and residual == 0 else (1 - residual / total if total else 0)
    return intercept, slope, r_squared


def _regression_projection(
    sample: Sequence[tuple[int, dict[str, int]]],
    corpus_pages: Sequence[int],
) -> dict[str, object]:
    distinct_pages = len({pages for pages, _ in sample})
    if len(sample) < 5 or distinct_pages < 5:
        return {
            "eligible": False,
            "reason": "requires_at_least_five_distinct_page_counts",
            "sample_documents": len(sample),
            "distinct_page_counts": distinct_pages,
            "projected_usage": None,
            "r_squared_total_tokens": None,
        }
    projected = {field: 0 for field in TOKEN_FIELDS}
    total_r_squared = 0.0
    coefficients: dict[str, dict[str, float]] = {}
    fits: dict[str, tuple[float, float]] = {}
    for field in TOKEN_FIELDS:
        points = [(pages, vector[field]) for pages, vector in sample]
        intercept, slope, r_squared = _linear_fit(points)
        coefficients[field] = {
            "intercept": round(intercept, 6),
            "tokens_per_page": round(slope, 6),
        }
        fits[field] = intercept, slope
        if field == "total_tokens":
            total_r_squared = r_squared
    for pages in corpus_pages:
        prediction = {
            field: max(0, round(fits[field][0] + fits[field][1] * pages))
            for field in ATOMIC_TOKEN_FIELDS
        }
        prediction["cache_write_input_tokens"] = min(
            prediction["cache_write_input_tokens"],
            prediction["cache_miss_input_tokens"],
        )
        prediction["reasoning_output_tokens"] = min(
            prediction["reasoning_output_tokens"], prediction["output_tokens"]
        )
        prediction["input_tokens"] = (
            prediction["cache_hit_input_tokens"]
            + prediction["cache_miss_input_tokens"]
            + prediction["unattributed_input_tokens"]
        )
        prediction["total_tokens"] = prediction["input_tokens"] + prediction["output_tokens"]
        for field in TOKEN_FIELDS:
            projected[field] += prediction[field]
    return {
        "eligible": True,
        "reason": None,
        "sample_documents": len(sample),
        "distinct_page_counts": distinct_pages,
        "projected_usage": projected,
        "r_squared_total_tokens": round(total_r_squared, 6),
        "coefficients": coefficients,
        "negative_document_predictions_clamped_to_zero": True,
    }


def build_pilot_report(
    documents: Sequence[Document],
    jobs: Sequence[IngestionJob],
    attempts: Sequence[IngestionAttempt],
    calls: Sequence[ModelCall],
    corpus: CorpusMeasurement,
    pricing: Pricing,
) -> dict[str, object]:
    document_ids = {document.id for document in documents}
    cohort_jobs = [job for job in jobs if job.document_id in document_ids]
    job_by_id = {job.id: job for job in cohort_jobs}
    cohort_attempts = [attempt for attempt in attempts if attempt.job_id in job_by_id]
    attempt_by_id = {attempt.id: attempt for attempt in cohort_attempts}
    cohort_calls = [
        call
        for call in calls
        if call.ingestion_attempt_id is not None and call.ingestion_attempt_id in attempt_by_id
    ]
    calls_by_attempt: defaultdict[object, list[ModelCall]] = defaultdict(list)
    for call in cohort_calls:
        calls_by_attempt[call.ingestion_attempt_id].append(call)

    attempt_counts = Counter(_enum_value(attempt.status) for attempt in cohort_attempts)
    job_counts = Counter(_enum_value(job.status) for job in cohort_jobs)
    artifact_counts = Counter(_enum_value(attempt.artifact_action) for attempt in cohort_attempts)
    attempts_per_job = Counter(attempt.job_id for attempt in cohort_attempts)
    jobs_per_document = Counter(job.document_id for job in cohort_jobs)
    legacy_jobs = [job for job in cohort_jobs if attempts_per_job[job.id] == 0]

    metadata_calls = [
        call
        for call in cohort_calls
        if _enum_value(call.stage) == ModelCallStage.ingestion_metadata.value
    ]
    pageindex_calls = [
        call
        for call in cohort_calls
        if _enum_value(call.stage) == ModelCallStage.pageindex_generation.value
    ]

    candidates: defaultdict[object, list[IngestionAttempt]] = defaultdict(list)
    for attempt in cohort_attempts:
        job = job_by_id[attempt.job_id]
        if (
            _enum_value(attempt.status) == IngestionAttemptStatus.completed.value
            and _enum_value(attempt.artifact_action)
            in {ArtifactAction.created.value, ArtifactAction.rebuilt.value}
            and attempt.telemetry_complete
            and attempt.page_count is not None
            and attempt.page_count > 0
            and calls_by_attempt[attempt.id]
        ):
            candidates[job.document_id].append(attempt)
    sample_attempts = [
        max(
            values,
            key=lambda item: (
                item.created_at or datetime.min.replace(tzinfo=UTC),
                item.attempt_number,
                str(item.id),
            ),
        )
        for values in candidates.values()
    ]
    sample_call_ids = {
        call.id for attempt in sample_attempts for call in calls_by_attempt[attempt.id]
    }
    sample_calls = [call for call in cohort_calls if call.id in sample_call_ids]
    sample_usage_payload, sample_vector = _usage_summary(sample_calls)
    sample_metadata_calls = [
        call
        for call in sample_calls
        if _enum_value(call.stage) == ModelCallStage.ingestion_metadata.value
    ]
    sample_pageindex_calls = [
        call
        for call in sample_calls
        if _enum_value(call.stage) == ModelCallStage.pageindex_generation.value
    ]
    _, sample_metadata_vector = _usage_summary(sample_metadata_calls)
    _, sample_pageindex_vector = _usage_summary(sample_pageindex_calls)
    sample_pages = [int(attempt.page_count or 0) for attempt in sample_attempts]
    sample_rows: list[tuple[int, dict[str, int]]] = []
    for attempt in sample_attempts:
        _, vector = _usage_summary(calls_by_attempt[attempt.id])
        sample_rows.append((int(attempt.page_count or 0), vector))

    coverage, missing_bands = _band_coverage(corpus.page_counts, sample_pages)
    page_ratio_projection: dict[str, object]
    if sample_pages and corpus.total_pages > 0:
        document_factor = len(corpus.page_counts) / len(sample_attempts)
        page_factor = corpus.total_pages / sum(sample_pages)
        projected_metadata_vector = _scale_vector(sample_metadata_vector, document_factor)
        projected_pageindex_vector = _scale_vector(sample_pageindex_vector, page_factor)
        projected_vector = _sum_vectors([projected_metadata_vector, projected_pageindex_vector])
        projected_payload = {"usage_available_calls": sample_usage_payload["usage_available_calls"]}
        page_ratio_projection = {
            "eligible": True,
            "sample_documents": len(sample_attempts),
            "corpus_documents": len(corpus.page_counts),
            "document_ratio": round(document_factor, 6),
            "sample_pages": sum(sample_pages),
            "corpus_pages": corpus.total_pages,
            "page_ratio": round(page_factor, 6),
            "projected_usage": projected_vector,
            "projected_cost": _price_usage(projected_payload, projected_vector, pricing),
            "by_stage": {
                "metadata": {
                    "projected_usage": projected_metadata_vector,
                    "projected_cost": _price_usage(
                        {
                            "usage_available_calls": sum(
                                call.usage_available for call in sample_metadata_calls
                            ),
                        },
                        projected_metadata_vector,
                        pricing,
                    ),
                },
                "pageindex": {
                    "projected_usage": projected_pageindex_vector,
                    "projected_cost": _price_usage(
                        {
                            "usage_available_calls": sum(
                                call.usage_available for call in sample_pageindex_calls
                            ),
                        },
                        projected_pageindex_vector,
                        pricing,
                    ),
                },
                "combined": {
                    "projected_usage": projected_vector,
                    "projected_cost": _price_usage(projected_payload, projected_vector, pricing),
                },
            },
        }
    else:
        projected_vector = {field: 0 for field in TOKEN_FIELDS}
        page_ratio_projection = {
            "eligible": False,
            "reason": "no_telemetry_complete_successful_rebuild_sample",
            "projected_usage": None,
            "projected_cost": None,
        }

    regression = _regression_projection(sample_rows, corpus.page_counts)
    if regression["projected_usage"] is not None:
        regression_vector = regression["projected_usage"]
        assert isinstance(regression_vector, dict)
        typed_regression_vector = cast(dict[str, object], regression_vector)
        regression["projected_cost"] = _price_usage(
            {"usage_available_calls": len(sample_calls)},
            {field: _payload_int(typed_regression_vector, field) for field in TOKEN_FIELDS},
            pricing,
        )
    else:
        regression["projected_cost"] = None

    _, all_vector = _usage_summary(cohort_calls)
    retry_calls = [call for call in cohort_calls if call.id not in sample_call_ids]
    retry_payload, retry_vector = _usage_summary(retry_calls)
    sample_attempt_ids = {attempt.id for attempt in sample_attempts}
    non_sample_attempts = [
        attempt for attempt in cohort_attempts if attempt.id not in sample_attempt_ids
    ]
    retry_telemetry_incomplete = (
        bool(legacy_jobs)
        or any(not attempt.telemetry_complete for attempt in non_sample_attempts)
        or any(_call_is_incomplete(call) for call in retry_calls)
    )
    retry_adjusted: dict[str, object]
    if not page_ratio_projection["eligible"] or sample_vector["total_tokens"] <= 0:
        retry_adjusted = {
            "eligible": False,
            "reason": "successful_projection_or_measured_sample_is_unavailable",
            "projected_usage": None,
            "projected_cost": None,
        }
    elif retry_telemetry_incomplete:
        retry_adjusted = {
            "eligible": False,
            "reason": "retry_telemetry_incomplete",
            "projected_usage": None,
            "projected_cost": None,
        }
    else:
        factor = all_vector["total_tokens"] / sample_vector["total_tokens"]
        adjusted_vector = _scale_vector(projected_vector, factor)
        retry_adjusted = {
            "eligible": True,
            "observed_overhead_factor": round(factor, 6),
            "projected_usage": adjusted_vector,
            "projected_cost": _price_usage(
                {"usage_available_calls": len(sample_calls)}, adjusted_vector, pricing
            ),
        }

    final_counts = Counter(_enum_value(document.status) for document in documents)
    confidence = (
        "low"
        if missing_bands or len(sample_attempts) < len(documents) or corpus.unreadable_files > 0
        else "moderate"
    )
    return {
        "schema_version": 2,
        "cohort": {
            "documents": len(documents),
            "final_states": dict(sorted(final_counts.items())),
        },
        "attempts": {
            "jobs": len(cohort_jobs),
            "job_statuses": dict(sorted(job_counts.items())),
            "attempts": len(cohort_attempts),
            "attempt_statuses": dict(sorted(attempt_counts.items())),
            "artifact_actions": dict(sorted(artifact_counts.items())),
            "retries": (
                sum(max(0, count - 1) for count in attempts_per_job.values())
                + sum(max(0, count - 1) for count in jobs_per_document.values())
            ),
            "provider_attempt_retries": sum(call.provider_attempt > 1 for call in cohort_calls),
            "failed_attempts": attempt_counts[IngestionAttemptStatus.failed.value],
            "abandoned_attempts": attempt_counts[IngestionAttemptStatus.abandoned.value],
            "legacy_jobs_without_attempts": len(legacy_jobs),
            "attempts_with_incomplete_telemetry": sum(
                not attempt.telemetry_complete for attempt in cohort_attempts
            ),
            "attempts_without_model_calls": sum(
                not calls_by_attempt[attempt.id] for attempt in cohort_attempts
            ),
            "legacy_or_unmeasured_jobs": sum(
                attempts_per_job[job.id] == 0
                or any(
                    not attempt.telemetry_complete
                    for attempt in cohort_attempts
                    if attempt.job_id == job.id
                )
                for job in cohort_jobs
            ),
        },
        "measured": {
            "metadata": _usage_and_cost(metadata_calls, pricing),
            "pageindex": _usage_and_cost(pageindex_calls, pricing),
            "combined": _usage_and_cost(cohort_calls, pricing),
        },
        "projection_sample": {
            "documents": len(sample_attempts),
            "pages": sum(sample_pages),
            "selection": "latest_telemetry_complete_created_or_rebuilt_attempt_per_document",
            "usage": {
                **sample_usage_payload,
                "cost": _price_usage(sample_usage_payload, sample_vector, pricing, sample_calls),
            },
        },
        "corpus": {
            "direct_pdf_count": corpus.direct_pdf_count,
            "readable_documents": len(corpus.page_counts),
            "total_pages": corpus.total_pages,
            "unreadable_files": corpus.unreadable_files,
            "page_count_coverage": coverage,
        },
        "projection": {
            "successful_index_page_ratio": page_ratio_projection,
            "linear_regression_sensitivity": regression,
            "observed_retry_overhead": {
                **retry_payload,
                "cost": _price_usage(retry_payload, retry_vector, pricing, retry_calls),
            },
            "retry_adjusted": retry_adjusted,
            "missing_page_bands": missing_bands,
            "confidence": confidence,
            "measured_and_projected_spend_are_separate": True,
        },
        "gate": {
            "full_corpus_approved": False,
            "approval_ready": False,
            "reason": "representative_pilot_required",
        },
    }


def build_query_cost_report(
    runs: Sequence[QueryRun],
    calls: Sequence[ModelCall],
    pricing: Pricing,
    *,
    days: int,
) -> dict[str, object]:
    calls_by_run: defaultdict[object, list[ModelCall]] = defaultdict(list)
    for call in calls:
        if call.query_run_id is not None:
            calls_by_run[call.query_run_id].append(call)
    grouped: defaultdict[tuple[str, str], list[QueryRun]] = defaultdict(list)
    for run in runs:
        route = _enum_value(run.route) if run.route is not None else "unselected"
        run_status = query_run_status(run)
        outcome = (
            _enum_value(run.failure_code)
            if run.failure_code is not None
            else (
                RunFailureCode.request_failed.value
                if run_status == QueryRunStatus.failed
                else run_status.value
            )
        )
        grouped[(route, outcome)].append(run)

    def run_is_incomplete(run: QueryRun) -> bool:
        run_calls = calls_by_run[run.id]
        run_status = query_run_status(run)
        if run_status == QueryRunStatus.running or any(
            _call_is_incomplete(call) for call in run_calls
        ):
            return True
        return not run_calls and not (
            run_status == QueryRunStatus.completed
            and run.route is not None
            and _enum_value(run.route) == RunRoute.help.value
            and is_static_capability_request(run.question)
        )

    groups: list[dict[str, object]] = []
    for (route, outcome), group_runs in sorted(grouped.items()):
        group_calls = [call for run in group_runs for call in calls_by_run[run.id]]
        incomplete_runs = sum(run_is_incomplete(run) for run in group_runs)
        usage_payload, usage_vector = _usage_summary(
            group_calls, empty_is_zero=incomplete_runs == 0
        )
        fully_known_run_costs: list[float] = []
        for run in group_runs:
            run_calls = calls_by_run[run.id]
            incomplete = run_is_incomplete(run)
            if not incomplete:
                run_payload, run_vector = _usage_summary(run_calls, empty_is_zero=True)
                priced = _price_usage(run_payload, run_vector, pricing, run_calls)
                known = priced["known_cost_usd"]
                if isinstance(known, float):
                    fully_known_run_costs.append(known)
        groups.append(
            {
                "route": route,
                "outcome": outcome,
                "runs": len(group_runs),
                "model_calls": len(group_calls),
                "incomplete_runs": incomplete_runs,
                "incomplete_calls": sum(_call_is_incomplete(call) for call in group_calls),
                "usage": usage_payload,
                "known_cost": _price_usage(usage_payload, usage_vector, pricing, group_calls),
                "average_known_cost_per_run_usd": (
                    round(sum(fully_known_run_costs) / len(fully_known_run_costs), 6)
                    if fully_known_run_costs
                    else None
                ),
                "runs_with_known_cost": len(fully_known_run_costs),
            }
        )
    return {
        "schema_version": 1,
        "window": {"days": days},
        "totals": {
            "runs": len(runs),
            "model_calls": len(calls),
            "incomplete_runs": sum(run_is_incomplete(run) for run in runs),
            "incomplete_calls": sum(_call_is_incomplete(call) for call in calls),
        },
        "groups": groups,
        "privacy": {
            "aggregate_only": True,
            "content_fields_included": False,
            "identity_fields_included": False,
        },
    }
