#!/usr/bin/env python3
"""Freeze a reviewed evaluation experiment and its deterministic trial order."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from vectorless_rag.config import get_settings
from vectorless_rag.evaluation_suite import (
    EVIDENCE_BUDGETS,
    FrozenEvaluationManifest,
    build_trials,
    evaluation_configuration_sha256,
    evaluation_price_snapshot,
    file_sha256,
    load_cases,
    load_corpus_manifest,
    validate_suite,
)
from vectorless_rag.llm import MAX_PROVIDER_ATTEMPTS

MAXIMUM_COST_USD = Decimal("99999999.999999")
COST_QUANTUM = Decimal("0.000001")


def git_value(*arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def cost_ceiling(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a decimal number") from error
    try:
        exactly_representable = parsed.quantize(COST_QUANTUM) == parsed
    except InvalidOperation:
        exactly_representable = False
    if (
        not parsed.is_finite()
        or parsed <= 0
        or parsed > MAXIMUM_COST_USD
        or not exactly_representable
    ):
        raise argparse.ArgumentTypeError(
            "must be positive, at most 99999999.999999, and use no more than 6 decimals"
        )
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent / "evaluation"
    parser.add_argument("--dataset", type=Path, default=root / "pageindex-v2-regression.jsonl")
    parser.add_argument("--corpus", type=Path, default=root / "pageindex-v2-corpus.json")
    parser.add_argument("--index-hash", required=True)
    parser.add_argument("--seed", type=int, default=20_260_725)
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        choices=(1, 2, 3),
        help="passes per item; more than one estimates within-configuration variance",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=cost_ceiling,
        required=True,
        help="database-enforced ceiling for the whole run; trials stop once it is reached",
    )
    parser.add_argument("--output", type=Path, default=root / "run")
    args = parser.parse_args()

    if git_value("status", "--porcelain"):
        raise SystemExit("evaluation execution requires a clean worktree")
    release_sha = git_value("rev-parse", "HEAD")
    cases = load_cases(args.dataset)
    corpus = load_corpus_manifest(args.corpus)
    validate_suite(cases, corpus, require_reviewed=True)
    settings = get_settings()
    if settings.release != release_sha:
        raise SystemExit("RELEASE must equal the clean worktree's HEAD commit")
    try:
        prices = evaluation_price_snapshot(
            settings, maximum_provider_attempts=MAX_PROVIDER_ATTEMPTS
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if len(args.index_hash) != 64 or any(
        value not in "0123456789abcdef" for value in args.index_hash
    ):
        raise SystemExit("--index-hash must be a lowercase SHA-256 digest")

    experiment_id = uuid.uuid4()
    manifest = FrozenEvaluationManifest(
        schema_version=1,
        experiment_id=experiment_id,
        release_sha=release_sha,
        configuration_sha256=evaluation_configuration_sha256(settings),
        corpus_sha256=corpus.corpus_snapshot_sha256,
        index_sha256=args.index_hash,
        dataset_sha256=file_sha256(args.dataset),
        price_snapshot=prices,
        repetitions=args.repetitions,
        evidence_budgets=EVIDENCE_BUDGETS,
        seed=args.seed,
        maximum_cost_usd=args.max_cost_usd,
        created_at=datetime.now(UTC),
    )
    trials = build_trials(cases, seed=args.seed, repetitions=args.repetitions)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "manifest.json").write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (args.output / "trials.jsonl").write_text(
        "".join(trial.model_dump_json() + "\n" for trial in trials),
        encoding="utf-8",
    )
    (args.output / "scores.jsonl").touch()
    print(
        json.dumps(
            {
                "experiment_id": str(experiment_id),
                "trials": len(trials),
                "output": str(args.output),
                "execution": "prepared",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
