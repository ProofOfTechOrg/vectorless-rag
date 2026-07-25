#!/usr/bin/env python3
"""Validate and upload an evaluation JSONL file to self-hosted Langfuse."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from langfuse import Langfuse

from vectorless_rag.evaluation_suite import (
    load_cases as load_pageindex_cases,
)
from vectorless_rag.evaluation_suite import (
    load_corpus_manifest,
    validate_suite,
)

MINIMUMS = {
    "single-document": 20,
    "cross-document": 15,
    "sql": 15,
    "hybrid": 15,
    "unanswerable": 10,
    "adversarial": 10,
}

SeedSuite = Literal["auto", "release-v1", "pageindex-v2"]


def load_release_cases(path: Path) -> list[dict[str, object]]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    counts = Counter(str(case.get("category")) for case in cases)
    missing = {
        name: count - counts[name] for name, count in MINIMUMS.items() if counts[name] < count
    }
    if missing:
        raise ValueError(f"dataset is below required category counts: {missing}")
    return cases


def load_seed_cases(
    path: Path,
    *,
    suite: SeedSuite,
    corpus: Path | None,
) -> list[dict[str, object]]:
    selected = (
        "pageindex-v2"
        if suite == "auto" and path.name == "pageindex-v2-regression.jsonl"
        else "release-v1"
        if suite == "auto"
        else suite
    )
    if selected == "release-v1":
        return load_release_cases(path)
    corpus_path = corpus or path.with_name("pageindex-v2-corpus.json")
    parsed = load_pageindex_cases(path)
    validate_suite(parsed, load_corpus_manifest(corpus_path), require_reviewed=True)
    return [
        {
            "item_id": str(case.item_id),
            "input": {
                "message": case.prompt,
                "constraints": case.constraints,
            },
            "expected_output": {
                "reference_answer": case.reference_answer,
                "answerable": case.answerable,
                "atomic_claims": [claim.claim for claim in case.atomic_claims],
                "support_sets": [value.model_dump(mode="json") for value in case.support_sets],
            },
            "split": case.split,
            "stratum": case.stratum,
        }
        for case in parsed
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--name", default="vectorless-rag-pageindex-v2-regression")
    parser.add_argument("--corpus", type=Path)
    parser.add_argument(
        "--suite",
        choices=("auto", "release-v1", "pageindex-v2"),
        default="auto",
    )
    args = parser.parse_args()
    cases = load_seed_cases(
        args.path,
        suite=args.suite,
        corpus=args.corpus,
    )
    client = Langfuse()
    dataset = client.create_dataset(name=args.name)
    for case in cases:
        client.create_dataset_item(
            id=str(case["item_id"]) if case.get("item_id") is not None else None,
            dataset_name=dataset.name,
            input=case["input"],
            expected_output=case.get("expected_output"),
            metadata={
                key: value for key, value in case.items() if key not in {"input", "expected_output"}
            },
        )
    client.flush()


if __name__ == "__main__":
    main()
