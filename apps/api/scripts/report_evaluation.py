#!/usr/bin/env python3
"""Build deterministic aggregate and paired-bootstrap evaluation metrics."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from vectorless_rag.evaluation_suite import (
    EvaluationScore,
    EvaluationTrial,
    FrozenEvaluationManifest,
    bootstrap_mean_interval,
)

METRICS = (
    "route_correct",
    "refusal_correct",
    "document_recall",
    "support_recall",
    "answer_claim_recall",
    "citation_precision",
)


def load_jsonl(path: Path, model: type[EvaluationTrial] | type[EvaluationScore]) -> list[object]:
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=Path("evaluation/run"))
    args = parser.parse_args()
    manifest = FrozenEvaluationManifest.model_validate_json(
        (args.run / "manifest.json").read_text(encoding="utf-8")
    )
    trials = {
        trial.trial_id: trial
        for trial in load_jsonl(args.run / "trials.jsonl", EvaluationTrial)
        if isinstance(trial, EvaluationTrial)
    }
    scores = [
        score
        for score in load_jsonl(args.run / "scores.jsonl", EvaluationScore)
        if isinstance(score, EvaluationScore)
    ]
    unknown = [str(score.trial_id) for score in scores if score.trial_id not in trials]
    if unknown:
        raise ValueError(f"scores refer to unknown trials: {unknown[:3]}")
    duplicates = Counter(score.trial_id for score in scores)
    if any(count > 1 for count in duplicates.values()):
        raise ValueError("scores contain duplicate trial IDs")

    grouped: defaultdict[tuple[str, int, str], list[EvaluationScore]] = defaultdict(list)
    for score in scores:
        if not score.included:
            continue
        trial = trials[score.trial_id]
        grouped[(trial.experiment_kind, trial.evidence_token_budget, trial.split)].append(score)
    aggregates: dict[str, object] = {}
    for group, values in sorted(grouped.items()):
        key = ":".join(map(str, group))
        row: dict[str, object] = {
            metric: bootstrap_mean_interval(
                [float(value) for score in values if (value := getattr(score, metric)) is not None],
                seed=manifest.seed,
            )
            for metric in METRICS
        }
        row["latency_ms"] = bootstrap_mean_interval(
            [float(score.latency_ms) for score in values if score.latency_ms is not None],
            seed=manifest.seed,
        )
        aggregates[key] = row

    # Failure codes only ever appear on excluded trials, so filtering on included made this
    # counter report "none" for every run no matter how many trials failed.
    failures = Counter(
        score.failure_code or "unclassified" for score in scores if not score.included
    )
    summary = {
        "schema_version": 1,
        "experiment_id": str(manifest.experiment_id),
        "planned_trials": len(trials),
        "scored_trials": len(scores),
        "included_trials": sum(score.included for score in scores),
        "excluded_trials": sum(not score.included for score in scores),
        "failures": dict(sorted(failures.items())),
        "aggregates": aggregates,
    }
    output = args.run / "summary.json"
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
