#!/usr/bin/env python3
"""Record claim judgments and apply them to a frozen evaluation score ledger."""

from __future__ import annotations

import argparse
import tempfile
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from vectorless_rag.evaluation_suite import (
    ClaimJudgeItem,
    ClaimJudgment,
    EvaluationScore,
    evaluation_ledger_lock,
)


class TrialModel(Protocol):
    trial_id: uuid.UUID


def load_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    if not path.exists():
        return []
    result: list[ModelT] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            result.append(model.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
    return result


def unique_by_trial[TrialT: TrialModel](
    values: list[TrialT],
    path: Path,
) -> dict[uuid.UUID, TrialT]:
    counts = Counter(value.trial_id for value in values)
    duplicates = [str(trial_id) for trial_id, count in counts.items() if count > 1]
    if duplicates:
        raise ValueError(f"{path.name} contains duplicate trial IDs: {duplicates[:3]}")
    return {value.trial_id: value for value in values}


def atomic_write_jsonl(path: Path, values: Sequence[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            for value in values:
                handle.write(value.model_dump_json() + "\n")
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_judgments(run: Path) -> tuple[int, int]:
    with evaluation_ledger_lock(run):
        return _apply_judgments_unlocked(run)


def _apply_judgments_unlocked(run: Path) -> tuple[int, int]:
    queue_path = run / "judge-queue.jsonl"
    scores_path = run / "scores.jsonl"
    judgments_path = run / "claim-judgments.jsonl"
    queued = unique_by_trial(load_jsonl(queue_path, ClaimJudgeItem), queue_path)
    scores = load_jsonl(scores_path, EvaluationScore)
    scores_by_trial = unique_by_trial(scores, scores_path)
    judgments = load_jsonl(judgments_path, ClaimJudgment)
    judgments_by_trial = unique_by_trial(judgments, judgments_path)

    unknown = sorted(str(trial_id) for trial_id in judgments_by_trial if trial_id not in queued)
    if unknown:
        raise ValueError(f"claim judgments refer to trials absent from the queue: {unknown[:3]}")
    if not judgments:
        return 0, len(queued)

    updates: dict[uuid.UUID, float] = {}
    for trial_id, judgment in judgments_by_trial.items():
        item = queued[trial_id]
        if judgment.item_id != item.item_id or judgment.claims != item.claims:
            raise ValueError(f"claim judgment does not match queued content: {trial_id}")
        if judgment.answer_sha256 != item.answer_sha256:
            raise ValueError(f"claim judgment does not match queued answer: {trial_id}")
        score = scores_by_trial.get(trial_id)
        if score is None:
            raise ValueError(f"claim judgment has no durable score: {trial_id}")
        if not score.included:
            raise ValueError(f"excluded trial cannot receive a claim judgment: {trial_id}")
        if score.answer_claim_recall is not None and score.answer_claim_recall != judgment.recall:
            raise ValueError(f"claim judgment conflicts with an existing score: {trial_id}")
        updates[trial_id] = judgment.recall

    updated = [
        score.model_copy(update={"answer_claim_recall": updates[score.trial_id]})
        if score.trial_id in updates
        else score
        for score in scores
    ]
    encoded = "".join(score.model_dump_json() + "\n" for score in updated)
    if scores_path.read_text(encoding="utf-8") != encoded:
        atomic_write_jsonl(scores_path, updated)
    return len(judgments), len(queued) - len(judgments)


def record_judgment(run: Path, judgment: ClaimJudgment) -> tuple[int, int]:
    with evaluation_ledger_lock(run):
        judgments_path = run / "claim-judgments.jsonl"
        judgments = load_jsonl(judgments_path, ClaimJudgment)
        existing = unique_by_trial(judgments, judgments_path).get(judgment.trial_id)
        if existing is not None:
            if existing != judgment:
                raise ValueError(
                    f"claim judgment conflicts with existing provenance: {judgment.trial_id}"
                )
        else:
            judgments.append(judgment)
            atomic_write_jsonl(judgments_path, judgments)
        return _apply_judgments_unlocked(run)


def render(item: ClaimJudgeItem, position: str) -> None:
    print("\n" + "=" * 78)
    print(f"{position}  trial={item.trial_id}  item={item.item_id}")
    print("-" * 78)
    print(item.answer)
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=Path("evaluation/run"))
    parser.add_argument("--reviewer")
    parser.add_argument("--method", choices=("human", "automated"), default="human")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    completed, remaining = apply_judgments(args.run)
    if args.status:
        print(f"completed  {completed:3d}")
        print(f"remaining  {remaining:3d}")
        return
    if not args.reviewer:
        raise SystemExit("--reviewer is required to record claim judgments")

    queue_path = args.run / "judge-queue.jsonl"
    judgments_path = args.run / "claim-judgments.jsonl"
    queued = load_jsonl(queue_path, ClaimJudgeItem)
    judgments = load_jsonl(judgments_path, ClaimJudgment)
    done = {judgment.trial_id for judgment in judgments}
    pending = [item for item in queued if item.trial_id not in done]
    if not pending:
        print("nothing to judge")
        return

    print("[y] claim is supported  [n] claim is not supported  [q] quit")
    for index, item in enumerate(pending, 1):
        render(item, f"[{index}/{len(pending)}]")
        decisions: list[bool] = []
        stopped = False
        for claim_number, claim in enumerate(item.claims, 1):
            print(f"\nCLAIM {claim_number}/{len(item.claims)}\n  {claim}")
            while True:
                choice = input("supported> ").strip().lower()
                if choice in {"y", "n", "q"}:
                    break
                print("expected y, n, or q")
            if choice == "q":
                stopped = True
                break
            decisions.append(choice == "y")
        if stopped:
            break
        judgment = ClaimJudgment(
            trial_id=item.trial_id,
            item_id=item.item_id,
            claims=item.claims,
            answer_sha256=item.answer_sha256,
            supported=decisions,
            reviewer=args.reviewer,
            method=args.method,
            reviewed_at=datetime.now(UTC),
        )
        record_judgment(args.run, judgment)


if __name__ == "__main__":
    main()
