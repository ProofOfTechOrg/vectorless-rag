#!/usr/bin/env python3
"""Validate and upload a release evaluation JSONL file to self-hosted Langfuse."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from langfuse import Langfuse

MINIMUMS = {
    "single-document": 20,
    "cross-document": 15,
    "sql": 15,
    "hybrid": 15,
    "unanswerable": 10,
    "adversarial": 10,
}


def load_cases(path: Path) -> list[dict[str, object]]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    counts = Counter(str(case.get("category")) for case in cases)
    missing = {
        name: count - counts[name] for name, count in MINIMUMS.items() if counts[name] < count
    }
    if missing:
        raise ValueError(f"dataset is below required category counts: {missing}")
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--name", default="vectorless-rag-release-v1")
    args = parser.parse_args()
    cases = load_cases(args.path)
    client = Langfuse()
    dataset = client.create_dataset(name=args.name)
    for case in cases:
        client.create_dataset_item(
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
