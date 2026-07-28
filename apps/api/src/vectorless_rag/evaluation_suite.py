from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVALUATION_NAMESPACE = uuid.UUID("54140b03-b887-5abf-b546-c619a055425c")
STRATA = (
    "explicit_single_document",
    "unconstrained_discovery",
    "metadata_constrained_discovery",
    "long_cross_section",
    "cross_document",
    "unanswerable",
)
EXPERIMENT_KINDS = (
    "route",
    "corpus_routing",
    "oracle_document_retrieval",
    "forced_route_end_to_end",
)
EVIDENCE_BUDGETS = (6_400, 12_800, 25_600)


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
        if self.status in {"approved", "edited", "rejected"} and (
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


class EvaluationCorpusManifest(StrictEvaluationModel):
    schema_version: Literal[1]
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    documents: list[CorpusDocument] = Field(min_length=1)


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
    created_at: datetime

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
        if require_reviewed and case.review.status not in {"approved", "edited"}:
            raise ValueError(f"case has no completed review: {case.item_id}")
        raw = case.model_dump(mode="json")
        if case.review.source_hash != case_source_hash(raw):
            raise ValueError(f"case review source hash is stale: {case.item_id}")
        target_ids = {target.arxiv_id for target in case.target_documents}
        split_targets[case.split].update(target_ids)
        if case.stratum in {
            "unconstrained_discovery",
            "metadata_constrained_discovery",
            "cross_document",
        }:
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
