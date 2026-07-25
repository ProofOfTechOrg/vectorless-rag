#!/usr/bin/env python3
"""Validate the tracked PageIndex evaluation suite without external services."""

from __future__ import annotations

import json
from pathlib import Path

from vectorless_rag.evaluation_suite import (
    EVIDENCE_BUDGETS,
    EXPERIMENT_KINDS,
    bootstrap_mean_interval,
    build_trials,
    load_cases,
    load_corpus_manifest,
    validate_suite,
)


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "evaluation"
    cases = load_cases(root / "pageindex-v2-regression.jsonl")
    result = validate_suite(
        cases,
        load_corpus_manifest(root / "pageindex-v2-corpus.json"),
    )
    trials = build_trials(cases, seed=20_260_725)
    repeated = build_trials(cases, seed=20_260_725)
    if trials != repeated:
        raise ValueError("evaluation trial ordering is not reproducible")
    expected = len(cases) * len(EXPERIMENT_KINDS) * len(EVIDENCE_BUDGETS) * 3
    if len(trials) != expected or len({trial.trial_id for trial in trials}) != expected:
        raise ValueError("evaluation trial plan is incomplete or contains duplicate IDs")
    interval = bootstrap_mean_interval([0.0, 0.5, 1.0], seed=20_260_725, draws=1_000)
    if interval != bootstrap_mean_interval([0.0, 0.5, 1.0], seed=20_260_725, draws=1_000):
        raise ValueError("evaluation bootstrap is not reproducible")
    result["planned_trials"] = len(trials)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
