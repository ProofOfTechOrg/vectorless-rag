from __future__ import annotations

import hashlib
import json
import math
import random
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vectorless_rag.config import Settings
from vectorless_rag.provider import DEEPSEEK_MAX_INPUT_TOKENS, DEEPSEEK_MAX_OUTPUT_TOKENS

EVALUATION_NAMESPACE = uuid.UUID("54140b03-b887-5abf-b546-c619a055425c")
STRATA = (
    "explicit_single_document",
    "unconstrained_discovery",
    "metadata_constrained_discovery",
    "long_cross_section",
    "cross_document",
    "unanswerable",
)
# Strata whose prompt must not name the document it is supposed to make the router find.
DISCOVERY_STRATA = frozenset(
    {"unconstrained_discovery", "metadata_constrained_discovery", "cross_document"}
)
# A review a human has finished, whichever way it went. Distinct from the statuses a suite
# may actually run with, which excludes rejected.
COMPLETED_REVIEW_STATUSES = frozenset({"approved", "edited", "rejected"})
USABLE_REVIEW_STATUSES = frozenset({"approved", "edited"})
EXPERIMENT_KINDS = (
    "route",
    "corpus_routing",
    "oracle_document_retrieval",
    "forced_route_end_to_end",
)
EVIDENCE_BUDGETS = (6_400, 12_800, 25_600)
PRICE_SNAPSHOT_KEYS = frozenset(
    {
        "input_per_million_usd",
        "cache_hit_input_per_million_usd",
        "output_per_million_usd",
        "max_input_tokens",
        "max_output_tokens",
        "max_attempts",
    }
)
EVALUATION_RUNTIME_SETTING_NAMES = (
    "deepseek_model",
    "pageindex_max_output_tokens",
    "query_node_selection_max_output_tokens",
    "pageindex_call_timeout_seconds",
    "request_deadline_seconds",
    "candidate_limit",
    "catalog_batch_token_limit",
    "document_limit",
    "page_ranges_per_document",
    "fetched_page_limit",
    "evidence_token_limit",
    "sql_row_limit",
    "sql_timeout_ms",
    "sql_lock_timeout_ms",
    "sql_payload_limit_bytes",
    "sql_explain_cost_ceiling",
)


def evaluation_configuration_sha256(settings: Settings) -> str:
    snapshot = {name: getattr(settings, name) for name in EVALUATION_RUNTIME_SETTING_NAMES}
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def evaluation_price_snapshot(
    settings: Settings, *, maximum_provider_attempts: int
) -> dict[str, float]:
    rates = {
        "input_per_million_usd": settings.deepseek_input_cost_per_million_usd,
        "cache_hit_input_per_million_usd": (settings.deepseek_cache_hit_input_cost_per_million_usd),
        "output_per_million_usd": settings.deepseek_output_cost_per_million_usd,
    }
    if any(value is None for value in rates.values()):
        raise ValueError("all three provider price settings are required")
    max_output_tokens = max(
        settings.pageindex_max_output_tokens,
        settings.query_node_selection_max_output_tokens,
    )
    if max_output_tokens > DEEPSEEK_MAX_OUTPUT_TOKENS:
        raise ValueError("configured output limit exceeds the provider maximum")
    return {
        **{key: float(value) for key, value in rates.items() if value is not None},
        "max_input_tokens": float(DEEPSEEK_MAX_INPUT_TOKENS),
        "max_output_tokens": float(max_output_tokens),
        "max_attempts": float(maximum_provider_attempts),
    }


def frozen_index_sha256(entries: Sequence[tuple[str | None, str]]) -> str:
    identifiers = [identifier for identifier, _ in entries]
    counts = Counter(identifiers)
    ambiguous = sorted(
        str(identifier) for identifier, count in counts.items() if identifier is None or count > 1
    )
    if ambiguous:
        raise ValueError(
            f"{len(ambiguous)} arXiv identifier(s) are missing or shared by several ready "
            f"documents, so the digest would not identify the index: {ambiguous[:5]}"
        )
    indexed = {str(identifier): digest for identifier, digest in entries}
    return hashlib.sha256(
        "\n".join(f"{arxiv_id}\0{indexed[arxiv_id]}" for arxiv_id in sorted(indexed)).encode()
    ).hexdigest()


class StrictEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AtomicClaim(StrictEvaluationModel):
    claim: str = Field(min_length=1, max_length=2_000)


class TargetDocument(StrictEvaluationModel):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}$")
    rank: int = Field(ge=1)


class SupportSpan(StrictEvaluationModel):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}$")
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    excerpt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def ordered(self) -> SupportSpan:
        if self.page_end < self.page_start:
            raise ValueError("support page range is reversed")
        return self


class SupportSet(StrictEvaluationModel):
    spans: list[SupportSpan] = Field(min_length=1)


class ReviewRecord(StrictEvaluationModel):
    status: Literal["pending", "approved", "edited", "rejected"]
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def approval_has_identity(self) -> ReviewRecord:
        if self.status in COMPLETED_REVIEW_STATUSES and (
            not self.reviewer or self.reviewed_at is None
        ):
            raise ValueError("completed reviews require reviewer and timestamp")
        return self


class EvaluationCase(StrictEvaluationModel):
    item_id: uuid.UUID
    split: Literal["dev", "regression"]
    stratum: Literal[
        "explicit_single_document",
        "unconstrained_discovery",
        "metadata_constrained_discovery",
        "long_cross_section",
        "cross_document",
        "unanswerable",
    ]
    prompt: str = Field(min_length=1, max_length=20_000)
    constraints: dict[str, object]
    atomic_claims: list[AtomicClaim]
    reference_answer: str
    answerable: bool
    target_documents: list[TargetDocument]
    support_sets: list[SupportSet]
    vocabulary_mismatch: bool
    exact_phrase: bool
    review: ReviewRecord

    @model_validator(mode="after")
    def grounded_shape(self) -> EvaluationCase:
        if self.answerable and (
            not self.atomic_claims or not self.target_documents or not self.support_sets
        ):
            raise ValueError("answerable cases require claims, targets, and support")
        if not self.answerable and (
            self.atomic_claims or self.target_documents or self.support_sets
        ):
            raise ValueError("unanswerable cases cannot declare target support")
        expected = stable_item_id(
            self.split,
            self.stratum,
            self.prompt,
            [target.arxiv_id for target in self.target_documents],
        )
        if self.item_id != expected:
            raise ValueError("evaluation item ID is not the stable UUIDv5")
        return self


class CorpusDocument(StrictEvaluationModel):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}$")
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)


def corpus_snapshot_sha256(documents: Sequence[CorpusDocument]) -> str:
    identifiers = [document.arxiv_id for document in documents]
    duplicates = sorted(
        identifier for identifier, count in Counter(identifiers).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"evaluation corpus repeats arXiv IDs: {duplicates[:5]}")
    return hashlib.sha256(
        "\n".join(
            f"{document.arxiv_id}\0{document.pdf_sha256}"
            for document in sorted(documents, key=lambda item: item.arxiv_id)
        ).encode()
    ).hexdigest()


class EvaluationCorpusManifest(StrictEvaluationModel):
    schema_version: Literal[1]
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    documents: list[CorpusDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def content_matches_snapshot(self) -> EvaluationCorpusManifest:
        if corpus_snapshot_sha256(self.documents) != self.corpus_snapshot_sha256:
            raise ValueError("evaluation corpus documents do not match its snapshot")
        return self


def validate_indexed_corpus(
    entries: Sequence[tuple[str | None, str]],
    corpus: EvaluationCorpusManifest,
) -> None:
    identifiers = [identifier for identifier, _ in entries]
    duplicates = sorted(
        str(identifier)
        for identifier, count in Counter(identifiers).items()
        if identifier is None or count > 1
    )
    if duplicates:
        raise ValueError(
            f"active index has missing or duplicate arXiv identifiers: {duplicates[:5]}"
        )
    indexed = {str(identifier): digest for identifier, digest in entries}
    expected = {document.arxiv_id: document.pdf_sha256 for document in corpus.documents}
    missing = sorted(set(expected) - set(indexed))
    mismatched = sorted(
        arxiv_id
        for arxiv_id, digest in expected.items()
        if indexed.get(arxiv_id) is not None and indexed[arxiv_id] != digest
    )
    if missing or mismatched:
        raise ValueError(
            "active index does not match the reviewed corpus PDFs: "
            f"missing={missing[:5]}, digest_mismatch={mismatched[:5]}"
        )


class FrozenEvaluationManifest(StrictEvaluationModel):
    schema_version: Literal[1]
    experiment_id: uuid.UUID
    release_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price_snapshot: dict[str, float]
    # Repetitions estimate within-configuration variance. A single pass still supports the
    # across-item bootstrap, so the count is recorded rather than fixed, and the report
    # carries it so a one-pass run is never mistaken for a repeated one.
    repetitions: int = Field(ge=1, le=3)
    evidence_budgets: tuple[Literal[6400, 12800, 25600], ...]
    seed: int
    # Frozen at plan time and materialised as the experiment's database ceiling before the
    # first trial, so an unattended run stops at a number chosen in advance rather than at
    # whatever the provider is willing to bill.
    maximum_cost_usd: Decimal = Field(
        gt=Decimal("0"),
        max_digits=14,
        decimal_places=6,
        allow_inf_nan=False,
    )
    created_at: datetime

    @field_validator("price_snapshot")
    @classmethod
    def complete_price_snapshot(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != set(PRICE_SNAPSHOT_KEYS):
            raise ValueError("evaluation price snapshot is incomplete")
        if any(not math.isfinite(item) or item < 0 for item in value.values()):
            raise ValueError("evaluation price snapshot values must be finite and non-negative")
        if any(
            value[key] <= 0
            for key in (
                "input_per_million_usd",
                "cache_hit_input_per_million_usd",
                "output_per_million_usd",
            )
        ):
            raise ValueError("evaluation provider prices must be positive")
        for key in ("max_input_tokens", "max_output_tokens", "max_attempts"):
            if value[key] < 1 or not value[key].is_integer():
                raise ValueError(f"{key} must be a positive integer")
        return value

    @model_validator(mode="after")
    def fixed_budgets(self) -> FrozenEvaluationManifest:
        if self.evidence_budgets != EVIDENCE_BUDGETS:
            raise ValueError("evaluation evidence budgets are not the frozen three-budget set")
        return self


class EvaluationTrial(StrictEvaluationModel):
    trial_id: uuid.UUID
    sequence_number: int = Field(ge=0)
    item_id: uuid.UUID
    split: Literal["dev", "regression"]
    stratum: Literal[
        "explicit_single_document",
        "unconstrained_discovery",
        "metadata_constrained_discovery",
        "long_cross_section",
        "cross_document",
        "unanswerable",
    ]
    experiment_kind: Literal[
        "route",
        "corpus_routing",
        "oracle_document_retrieval",
        "forced_route_end_to_end",
    ]
    evidence_token_budget: Literal[6400, 12800, 25600]
    repetition: int = Field(ge=1, le=3)


class EvaluationScore(StrictEvaluationModel):
    trial_id: uuid.UUID
    query_run_id: uuid.UUID | None = None
    route_correct: float | None = Field(default=None, ge=0, le=1)
    # Unanswerable cases only: whether the run refused instead of citing. Kept apart from the
    # recall metrics so a refusal is never averaged into a ratio over target spans.
    refusal_correct: float | None = Field(default=None, ge=0, le=1)
    document_recall: float | None = Field(default=None, ge=0, le=1)
    support_recall: float | None = Field(default=None, ge=0, le=1)
    answer_claim_recall: float | None = Field(default=None, ge=0, le=1)
    citation_precision: float | None = Field(default=None, ge=0, le=1)
    latency_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    failure_code: str | None = Field(default=None, max_length=80)
    included: bool = True


def stable_item_id(
    split: str,
    stratum: str,
    prompt: str,
    target_ids: list[str],
) -> uuid.UUID:
    identity = json.dumps(
        {
            "split": split,
            "stratum": stratum,
            "prompt": prompt,
            "target_ids": target_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(EVALUATION_NAMESPACE, identity)


def case_source_hash(value: dict[str, object]) -> str:
    source = {key: item for key, item in value.items() if key not in {"item_id", "review"}}
    encoded = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_trials(
    cases: list[EvaluationCase],
    *,
    seed: int,
    repetitions: int = 1,
) -> list[EvaluationTrial]:
    if not 1 <= repetitions <= 3:
        raise ValueError("evaluation repetitions must be between one and three")
    values: list[tuple[EvaluationCase, str, int, int]] = []
    for repetition in range(1, repetitions + 1):
        for budget in EVIDENCE_BUDGETS:
            for kind in EXPERIMENT_KINDS:
                for case in cases:
                    values.append((case, kind, budget, repetition))
    random.Random(seed).shuffle(values)  # noqa: S311 - reproducibility is intentional
    result: list[EvaluationTrial] = []
    for sequence, (case, kind, budget, repetition) in enumerate(values):
        identity = f"{case.item_id}:{kind}:{budget}:{repetition}"
        result.append(
            EvaluationTrial(
                trial_id=uuid.uuid5(EVALUATION_NAMESPACE, identity),
                sequence_number=sequence,
                item_id=case.item_id,
                split=case.split,
                stratum=case.stratum,
                experiment_kind=kind,  # type: ignore[arg-type]
                evidence_token_budget=budget,  # type: ignore[arg-type]
                repetition=repetition,
            )
        )
    return result


def bootstrap_mean_interval(
    values: list[float],
    *,
    seed: int,
    draws: int = 10_000,
) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "lower_95": None, "upper_95": None}
    generator = random.Random(seed)  # noqa: S311 - deterministic bootstrap is required
    means = sorted(
        sum(generator.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "lower_95": means[int(draws * 0.025)],
        "upper_95": means[min(draws - 1, int(draws * 0.975))],
    }


def load_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(EvaluationCase.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
    return cases


def load_corpus_manifest(path: Path) -> EvaluationCorpusManifest:
    return EvaluationCorpusManifest.model_validate_json(path.read_text(encoding="utf-8"))


def validate_suite(
    cases: list[EvaluationCase],
    corpus: EvaluationCorpusManifest,
    *,
    require_reviewed: bool = False,
) -> dict[str, object]:
    if len(cases) != 120:
        raise ValueError(f"evaluation suite must contain 120 cases, found {len(cases)}")
    if len({case.item_id for case in cases}) != len(cases):
        raise ValueError("evaluation item IDs are not unique")
    split_counts = Counter(case.split for case in cases)
    if split_counts != {"dev": 48, "regression": 72}:
        raise ValueError(f"invalid split counts: {dict(split_counts)}")
    stratum_counts = Counter(case.stratum for case in cases)
    if any(stratum_counts[stratum] != 20 for stratum in STRATA):
        raise ValueError(f"invalid stratum counts: {dict(stratum_counts)}")
    if sum(case.vocabulary_mismatch for case in cases) < 24:
        raise ValueError("evaluation suite requires at least 24 vocabulary-mismatch cases")
    if sum(case.exact_phrase for case in cases) < 12:
        raise ValueError("evaluation suite requires at least 12 exact-phrase cases")
    manifest = {document.arxiv_id: document for document in corpus.documents}
    split_targets: defaultdict[str, set[str]] = defaultdict(set)
    for case in cases:
        if require_reviewed and case.review.status not in USABLE_REVIEW_STATUSES:
            raise ValueError(f"case has no completed review: {case.item_id}")
        raw = case.model_dump(mode="json")
        if case.review.source_hash != case_source_hash(raw):
            raise ValueError(f"case review source hash is stale: {case.item_id}")
        target_ids = {target.arxiv_id for target in case.target_documents}
        split_targets[case.split].update(target_ids)
        if case.stratum in DISCOVERY_STRATA:
            normalized_prompt = case.prompt.casefold()
            if any(target_id.casefold() in normalized_prompt for target_id in target_ids):
                raise ValueError(f"discovery prompt leaks a target ID: {case.item_id}")
        for support_set in case.support_sets:
            for span in support_set.spans:
                document = manifest.get(span.arxiv_id)
                if document is None:
                    raise ValueError(f"support document is absent from manifest: {span.arxiv_id}")
                if span.page_end > document.page_count:
                    raise ValueError(f"support page is out of bounds: {case.item_id}")
    overlap = split_targets["dev"] & split_targets["regression"]
    if overlap:
        raise ValueError(f"dev/regression target documents overlap: {sorted(overlap)}")
    review_counts = Counter(case.review.status for case in cases)
    return {
        "cases": len(cases),
        "splits": dict(split_counts),
        "strata": dict(stratum_counts),
        "vocabulary_mismatch": sum(case.vocabulary_mismatch for case in cases),
        "exact_phrase": sum(case.exact_phrase for case in cases),
        "reviews": dict(review_counts),
        "corpus_documents": len(corpus.documents),
    }
