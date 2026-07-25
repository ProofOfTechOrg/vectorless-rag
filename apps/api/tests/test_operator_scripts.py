from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from scripts import run_pageindex_pilot, seed_evaluation  # noqa: E402


def test_pilot_decimal_cost_is_json_serializable() -> None:
    reported = run_pageindex_pilot.report_cost(Decimal("1.2345678"))

    assert reported == 1.234568
    assert json.loads(json.dumps({"cost": reported})) == {"cost": 1.234568}


def test_explicit_pageindex_suite_does_not_depend_on_stdin_filename(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path]] = []
    corpus = Path("evaluation/pageindex-v2-corpus.json")

    def load_cases(path: Path) -> list[Any]:
        calls.append(("cases", path))
        return []

    def load_corpus(path: Path) -> object:
        calls.append(("corpus", path))
        return object()

    def validate(cases: object, manifest: object, *, require_reviewed: bool) -> None:
        del cases, manifest
        assert require_reviewed is True

    def fail_release(path: Path) -> list[dict[str, object]]:
        pytest.fail(f"selected release loader for {path}")

    monkeypatch.setattr(seed_evaluation, "load_pageindex_cases", load_cases)
    monkeypatch.setattr(seed_evaluation, "load_corpus_manifest", load_corpus)
    monkeypatch.setattr(seed_evaluation, "validate_suite", validate)
    monkeypatch.setattr(seed_evaluation, "load_release_cases", fail_release)

    cases = seed_evaluation.load_seed_cases(
        Path("/dev/stdin"),
        suite="pageindex-v2",
        corpus=corpus,
    )

    assert cases == []
    assert calls == [("cases", Path("/dev/stdin")), ("corpus", corpus)]


def test_seed_auto_mode_preserves_legacy_filename_selection(
    monkeypatch: MonkeyPatch,
) -> None:
    expected: list[dict[str, object]] = [{"item_id": "legacy"}]

    def load_release(path: Path) -> list[dict[str, object]]:
        del path
        return expected

    def fail_pageindex(path: Path) -> list[Any]:
        pytest.fail(f"selected PageIndex loader for {path}")

    monkeypatch.setattr(seed_evaluation, "load_release_cases", load_release)
    monkeypatch.setattr(seed_evaluation, "load_pageindex_cases", fail_pageindex)

    assert (
        seed_evaluation.load_seed_cases(
            Path("release-v1.jsonl"),
            suite="auto",
            corpus=None,
        )
        == expected
    )


def test_existing_pilot_output_blocks_before_model_work(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "run" / "result.json"
    output.parent.mkdir()
    called = False

    async def fake_run(args: object) -> dict[str, object]:
        del args
        nonlocal called
        called = True
        return {}

    def load_manifest(path: Path) -> None:
        del path

    monkeypatch.setattr(run_pageindex_pilot, "load_pilot_manifest", load_manifest)
    monkeypatch.setattr(run_pageindex_pilot, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pageindex_pilot.py",
            "--apply",
            "--confirm-cost-cap",
            "10",
            "--release-sha",
            "a" * 40,
            "--output",
            str(output),
        ],
    )

    with pytest.raises(FileExistsError):
        run_pageindex_pilot.main()

    assert called is False


def test_pilot_reserves_output_before_running(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "run" / "result.json"

    async def fake_run(args: object) -> dict[str, object]:
        del args
        assert output.parent.is_dir()
        assert (output.parent / ".result.json.tmp").is_file()
        return {"release_sha": "a" * 40}

    def load_manifest(path: Path) -> None:
        del path

    monkeypatch.setattr(run_pageindex_pilot, "load_pilot_manifest", load_manifest)
    monkeypatch.setattr(run_pageindex_pilot, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pageindex_pilot.py",
            "--apply",
            "--confirm-cost-cap",
            "10",
            "--release-sha",
            "a" * 40,
            "--output",
            str(output),
        ],
    )

    run_pageindex_pilot.main()

    assert output.read_text(encoding="utf-8").endswith("\n")
    assert not (output.parent / ".result.json.tmp").exists()
