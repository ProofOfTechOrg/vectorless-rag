from __future__ import annotations

import json
import os
import re
import stat
import subprocess
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


def test_environment_initializer_is_atomic_private_and_non_overwriting(tmp_path: Path) -> None:
    root = API_ROOT.parents[1]
    template = root / ".env.example"
    output = tmp_path / ".env"
    command = [
        sys.executable,
        str(root / "scripts/init_env.py"),
        "--template",
        str(template),
        "--output",
        str(output),
    ]

    processes = [
        subprocess.Popen(  # noqa: S603
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]
    return_codes = sorted(process.wait() for process in processes)

    assert return_codes == [0, 2]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".env.tmp-*"))

    assignments = dict(
        line.split("=", maxsplit=1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert assignments["DEEPSEEK_API_KEY"] == ""

    hex_keys = {
        "SESSION_SECRET",
        "POSTGRES_PASSWORD",
        "RAG_OWNER_PASSWORD",
        "RAG_APP_PASSWORD",
        "RAG_READER_PASSWORD",
        "API_KEY_PEPPER",
        "MINIO_ROOT_PASSWORD",
        "CLICKHOUSE_PASSWORD",
        "REDIS_PASSWORD",
        "LANGFUSE_DB_PASSWORD",
        "LANGFUSE_NEXTAUTH_SECRET",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
    }
    for key in hex_keys:
        assert re.fullmatch(r"[0-9a-f]{64}", assignments[key]), key
    assert re.fullmatch(r"lf_pk_[0-9a-f]{64}", assignments["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"])
    assert re.fullmatch(r"lf_sk_[0-9a-f]{64}", assignments["LANGFUSE_INIT_PROJECT_SECRET_KEY"])
    assert assignments["LANGFUSE_INIT_USER_PASSWORD"] == ""

    combined_output = "\n".join(value for result in results for value in result)
    for key in hex_keys:
        assert assignments[key] not in combined_output

    original = output.read_bytes()
    repeated = subprocess.run(  # noqa: S603
        command, text=True, capture_output=True, check=False
    )
    assert repeated.returncode == 2
    assert output.read_bytes() == original
    assert "already exists; leaving it unchanged" in repeated.stderr


def test_integration_runner_cleans_only_its_project_after_failure(tmp_path: Path) -> None:
    root = API_ROOT.parents[1]
    call_log = tmp_path / "docker-calls.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "CALL_LOG": str(call_log),
            "DOCKER": str(fake_docker),
            "UV": "/bin/false",
            "MAKE": "/bin/true",
            "TEST_POSTGRES_PORT": "55439",
        }
    )
    result = subprocess.run(  # noqa: S603
        [str(root / "scripts/test-integration-stack.sh")],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    project_matches = [
        re.search(r"--project-name (vectorless-rag-integration-[^ ]+)", call) for call in calls
    ]
    assert all(match is not None for match in project_matches)
    project_names = [match.group(1) for match in project_matches if match is not None]
    assert len(set(project_names)) == 1
    assert "up -d --wait postgres" in calls[0]
    assert "down --volumes --remove-orphans" in calls[1]
    assert "--project-name vectorless-rag " not in "\n".join(calls)
