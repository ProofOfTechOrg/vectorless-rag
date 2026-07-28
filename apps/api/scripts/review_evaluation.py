#!/usr/bin/env python3
"""Review the tracked PageIndex evaluation suite one case at a time.

Recomputes the review source hash, and the stable item ID when an edit changes the
prompt or targets, so a completed review always satisfies the suite validator.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from vectorless_rag.evaluation_suite import (
    COMPLETED_REVIEW_STATUSES,
    DISCOVERY_STRATA,
    STRATA,
    EvaluationCase,
    case_source_hash,
    stable_item_id,
)

DATASET = Path(__file__).resolve().parent.parent / "evaluation/pageindex-v2-regression.jsonl"
SOURCE = Path(__file__).resolve().parent.parent / "evaluation/pageindex-v2-review-source.json"


def load(path: Path) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def save(path: Path, cases: list[dict[str, Any]], expected_mtime: float) -> float:
    """Write through a temporary file so an interrupted review cannot truncate the suite.

    Refuses to write when the file changed underneath us. The whole suite is rewritten on
    every decision, so a second reviewer - or a dataset rebuild - would otherwise be
    silently erased by whichever process saved last.
    """
    if path.stat().st_mtime != expected_mtime:
        raise SystemExit(
            f"{path.name} changed outside this session; decisions already recorded are "
            "safe, but this one was not written. Re-run to continue from the current file."
        )
    mode = path.stat().st_mode & 0o777
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            for case in cases:
                handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
        # NamedTemporaryFile creates at 0600 and replace() keeps the temp file's mode, which
        # would silently narrow the tracked dataset's permissions on every save.
        temporary.chmod(mode)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path.stat().st_mtime


def reseal(case: dict[str, Any]) -> None:
    """Restore the identity and hash invariants the suite validator enforces."""
    case["item_id"] = str(
        stable_item_id(
            str(case["split"]),
            str(case["stratum"]),
            str(case["prompt"]),
            [str(target["arxiv_id"]) for target in case["target_documents"]],
        )
    )
    # case_source_hash already excludes item_id and review from the digest.
    case["review"]["source_hash"] = case_source_hash(case)


def rejected_reason(case: dict[str, Any], cases: list[dict[str, Any]], index: int) -> str | None:
    """Reject an edit the suite validator would refuse, while it can still be undone.

    Without this the failure surfaces much later, from a separate `make evaluation-check`,
    by which point the offending edit is one of many already written to the tracked file.
    """
    try:
        EvaluationCase.model_validate(case)
    except ValidationError as error:
        return str(error).splitlines()[-1].strip()
    # reference_answer carries no min_length on the model, so an emptied buffer would
    # otherwise save an empty ground truth and pass every downstream check.
    if not str(case["reference_answer"]).strip():
        return "the reference answer cannot be empty"
    if any(
        other["item_id"] == case["item_id"]
        for position, other in enumerate(cases)
        if position != index
    ):
        return "the edited prompt collides with another case's item ID"
    if case["stratum"] in DISCOVERY_STRATA:
        prompt = str(case["prompt"]).casefold()
        leaked = [
            str(target["arxiv_id"])
            for target in case["target_documents"]
            if str(target["arxiv_id"]).casefold() in prompt
        ]
        if leaked:
            return f"a discovery prompt must not name its target: {', '.join(leaked)}"
    return None


def render(case: dict[str, Any], excerpts: list[dict[str, Any]], position: str) -> None:
    print("\n" + "=" * 78)
    print(f"{position}  {case['item_id']}")
    print(
        f"split={case['split']}  stratum={case['stratum']}  "
        f"answerable={case['answerable']}  vocab_mismatch={case['vocabulary_mismatch']}"
    )
    print(f"constraints: {json.dumps(case['constraints'])}")
    review = cast(dict[str, Any], case["review"])
    if review["status"] != "pending":
        print(
            f"PRIOR: {review['status']} by {review['reviewer']} at {review['reviewed_at']}"
            + (f" - {review['note']}" if review["note"] else "")
        )
    print("-" * 78)
    print(f"PROMPT\n  {case['prompt']}")
    print(f"\nREFERENCE\n  {case['reference_answer']}")
    for excerpt in excerpts:
        print(f"\nSOURCE {excerpt['arxiv_id']} p{excerpt['page']}\n  {excerpt['excerpt']}")
    print("=" * 78)


def edit_text(current: str) -> str | None:
    """Open the value in $EDITOR. Returns None when the buffer comes back unchanged."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
        handle.write(current)
        path = Path(handle.name)
    try:
        # $EDITOR commonly carries flags ("code --wait"), so resolve only the binary.
        command = shlex.split(os.environ.get("EDITOR") or "nano")
        resolved = shutil.which(command[0]) if command else None
        if resolved is None:
            raise SystemExit(f"editor not found on PATH: {command[:1]}; set $EDITOR")
        try:
            # noqa justified: resolved comes from shutil.which on the operator's own $EDITOR.
            subprocess.run([resolved, *command[1:], str(path)], check=True)  # noqa: S603
        except subprocess.CalledProcessError as error:
            raise SystemExit(f"editor exited with status {error.returncode}") from error
        value = path.read_text(encoding="utf-8").strip()
    finally:
        path.unlink(missing_ok=True)
    return None if value == current.strip() else value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer", help="reviewer identity recorded on each decision")
    parser.add_argument("--status", action="store_true", help="print progress and exit")
    parser.add_argument("--stratum", help="only walk these strata (comma-separated)")
    parser.add_argument("--item", help="only walk this item ID")
    parser.add_argument(
        "--include-done", action="store_true", help="revisit cases already reviewed"
    )
    args = parser.parse_args()

    cases = load(DATASET)
    mtime = DATASET.stat().st_mtime
    if args.status:
        counts = Counter(case["review"]["status"] for case in cases)
        for status, total in sorted(counts.items()):
            print(f"{status:10s} {total:3d}")
        print(f"{'TOTAL':10s} {len(cases):3d}")
        remaining = sum(
            total for status, total in counts.items() if status not in COMPLETED_REVIEW_STATUSES
        )
        print(f"\n{remaining} case(s) still need a decision.")
        return

    if not args.reviewer:
        raise SystemExit("--reviewer is required to record a decision")

    excerpts = {row["item_id"]: row["excerpts"] for row in json.loads(SOURCE.read_text("utf-8"))}
    wanted: set[str] = (
        {value.strip() for value in str(args.stratum).split(",")} if args.stratum else set()
    )
    unknown = sorted(wanted - set(STRATA))
    if unknown:
        raise SystemExit(f"unknown stratum: {', '.join(unknown)}")
    queue = [
        index
        for index, case in enumerate(cases)
        if (args.include_done or case["review"]["status"] not in COMPLETED_REVIEW_STATUSES)
        and (not wanted or case["stratum"] in wanted)
        and (not args.item or case["item_id"] == args.item)
    ]
    if not queue:
        print("nothing to review")
        return

    print("[a]pprove  [e]dit reference  [p]rompt edit  [r]eject  [s]kip  [q]uit and save")
    for order, index in enumerate(queue, 1):
        case = cases[index]
        render(case, excerpts.get(str(case["item_id"]), []), f"[{order}/{len(queue)}]")

        restore = dict(case)
        try:
            while True:
                choice = input("decision> ").strip().lower()
                if choice in {"a", "e", "p", "r", "s", "q"}:
                    break
                print("expected one of a/e/p/r/s/q")

            if choice in {"q", "s"}:
                if choice == "s":
                    continue
                break

            note = None
            if choice in {"e", "p"}:
                field = "reference_answer" if choice == "e" else "prompt"
                replacement = edit_text(str(case[field]))
                if replacement is None:
                    print("  buffer unchanged; leaving the case pending")
                    continue
                case[field] = replacement
                status = "edited"
            elif choice == "r":
                status = "rejected"
                while not (note := input("reason (required for a reject)> ").strip()):
                    print("  a reject needs a reason")
                print("  note: rejected cases fail the preflight; fix or replace before seeding")
            else:
                status = "approved"
            if note is None:
                note = input("note (optional)> ").strip() or None
        except (EOFError, KeyboardInterrupt):
            # Abandon the half-made decision; every completed one is already on disk.
            cases[index] = restore
            print("\ninterrupted")
            break

        previous_id = str(case["item_id"])
        case["review"] = {
            "status": status,
            "reviewer": args.reviewer,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "note": note,
        }
        reseal(case)
        problem = rejected_reason(case, cases, index)
        if problem is not None:
            cases[index] = restore
            print(f"  edit refused: {problem}")
            continue
        # A prompt edit moves item_id, so re-key the excerpts this case renders from.
        if case["item_id"] != previous_id:
            excerpts[str(case["item_id"])] = excerpts.pop(previous_id, [])
        mtime = save(DATASET, cases, mtime)
        print(f"  recorded {status}")

    counts = Counter(case["review"]["status"] for case in cases)
    print("\n" + "  ".join(f"{status}={total}" for status, total in sorted(counts.items())))
    print("run `make evaluation-check` to validate")


if __name__ == "__main__":
    main()
