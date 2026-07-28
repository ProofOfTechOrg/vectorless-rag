#!/usr/bin/env python3
"""Freeze a reviewed evaluation experiment and its deterministic trial order."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from vectorless_rag.config import get_settings
from vectorless_rag.evaluation_suite import (
    EVIDENCE_BUDGETS,
    FrozenEvaluationManifest,
    build_trials,
    file_sha256,
    load_cases,
    load_corpus_manifest,
    validate_suite,
)
from vectorless_rag.provider import provider_configuration_hash


def git_value(*arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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
    parser.add_argument("--output", type=Path, default=root / "run")
    args = parser.parse_args()

    if git_value("status", "--porcelain"):
        raise SystemExit("evaluation execution requires a clean worktree")
    release_sha = git_value("rev-parse", "HEAD")
    cases = load_cases(args.dataset)
    corpus = load_corpus_manifest(args.corpus)
    validate_suite(cases, corpus, require_reviewed=True)
    settings = get_settings()
    prices = {
        "input_per_million_usd": settings.deepseek_input_cost_per_million_usd,
        "cache_hit_input_per_million_usd": (settings.deepseek_cache_hit_input_cost_per_million_usd),
        "output_per_million_usd": settings.deepseek_output_cost_per_million_usd,
    }
    if any(value is None for value in prices.values()):
        raise SystemExit("all three provider price settings are required")
    if len(args.index_hash) != 64 or any(
        value not in "0123456789abcdef" for value in args.index_hash
    ):
        raise SystemExit("--index-hash must be a lowercase SHA-256 digest")

    experiment_id = uuid.uuid4()
    manifest = FrozenEvaluationManifest(
        schema_version=1,
        experiment_id=experiment_id,
        release_sha=release_sha,
        configuration_sha256=provider_configuration_hash(settings),
        corpus_sha256=corpus.corpus_snapshot_sha256,
        index_sha256=args.index_hash,
        dataset_sha256=file_sha256(args.dataset),
        price_snapshot={key: float(value) for key, value in prices.items() if value is not None},
        repetitions=args.repetitions,
        evidence_budgets=EVIDENCE_BUDGETS,
        seed=args.seed,
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
