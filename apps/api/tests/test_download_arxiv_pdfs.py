from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "apps/api/scripts/download-arxiv-pdfs.sh"
MANIFEST = REPOSITORY_ROOT / "apps/api/scripts/arxiv-pdf-ids.txt"
EVALUATION_DATASET = REPOSITORY_ROOT / "apps/api/evaluation/release-v1.jsonl"
EXPECTED_MANIFEST_SHA256 = "101c8edcd1ea9cbbe8a216e031de039413f3d8263f8118c05e6e2f190853b744"


def identifiers() -> list[str]:
    return MANIFEST.read_text().splitlines()


def write_pdf(path: Path) -> None:
    _ = path.write_bytes(b"%PDF-1.7\n")


def prepare_destination(destination: Path, missing: set[str]) -> None:
    destination.mkdir(parents=True)
    for identifier in identifiers():
        if identifier not in missing:
            write_pdf(destination / f"{identifier}.pdf")


def create_fake_curl(tmp_path: Path) -> tuple[Path, Path]:
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    executable = executable_directory / "curl"
    _ = executable.write_text(
        """#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
output = Path(arguments[arguments.index("--output") + 1])
identifier = arguments[-1].rsplit("/", 1)[-1]
with Path(os.environ["FAKE_CURL_LOG"]).open("a", encoding="utf-8") as log:
    log.write(identifier + "\\n")

mode = os.environ.get("FAKE_CURL_MODE", "pdf")
if mode == "fail" or identifier == os.environ.get("FAKE_CURL_FAIL_ID"):
    raise SystemExit(22)
if mode == "wait":
    output.write_bytes(b"%PDF-partial")
    time.sleep(30)
if mode == "race":
    Path(os.environ["FAKE_CURL_RACE_TARGET"]).write_bytes(b"%PDF-existing\\n")
if mode == "html":
    output.write_text("<html>not a PDF</html>")
else:
    output.write_bytes(b"%PDF-1.7\\n")
"""
    )
    executable.chmod(0o755)
    return executable_directory, tmp_path / "curl.log"


def run_downloader(
    destination: Path,
    executable_directory: Path,
    log: Path,
    **environment_overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = downloader_environment(executable_directory, log, **environment_overrides)
    return subprocess.run(  # noqa: S603
        [str(SCRIPT), str(destination)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def downloader_environment(
    executable_directory: Path,
    log: Path,
    **environment_overrides: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = f"{executable_directory}{os.pathsep}{environment['PATH']}"
    environment["FAKE_CURL_LOG"] = str(log)
    environment.update(environment_overrides)
    return environment


def logged_identifiers(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def evaluation_arxiv_ids() -> set[str]:
    result: set[str] = set()
    for line in EVALUATION_DATASET.read_text().splitlines():
        row = cast(dict[str, object], json.loads(line))
        request = cast(dict[str, object], row["input"])
        constraints = cast(dict[str, object], request["constraints"])
        values = cast(list[str], constraints["arxiv_ids"])
        result.update(identifier for identifier in values if not identifier.startswith("9999."))
    return result


def test_manifest_defines_the_complete_corpus() -> None:
    content = MANIFEST.read_bytes()
    values = identifiers()

    assert hashlib.sha256(content).hexdigest() == EXPECTED_MANIFEST_SHA256
    assert len(values) == 537
    assert values == sorted(values)
    assert len(values) == len(set(values))
    assert all(re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}", identifier) for identifier in values)
    assert evaluation_arxiv_ids() <= set(values)


def test_script_has_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603
        [bash, "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_user_agent_identifies_current_repository() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert (
        'user_agent="vectorless-rag (+https://github.com/ProofOfTechOrg/vectorless-rag)"' in script
    )


def test_downloads_missing_pdf_and_rerun_skips_everything(tmp_path: Path) -> None:
    values = identifiers()
    missing = values[-1]
    destination = tmp_path / "corpus"
    prepare_destination(destination, {missing})
    executable_directory, log = create_fake_curl(tmp_path)

    first = run_downloader(destination, executable_directory, log)
    assert first.returncode == 0, first.stderr
    assert logged_identifiers(log) == [missing]
    assert (destination / f"{missing}.pdf").read_bytes().startswith(b"%PDF-")
    assert "downloaded=1 skipped=536 failed=0" in first.stdout

    second = run_downloader(
        destination,
        executable_directory,
        log,
        FAKE_CURL_MODE="fail",
    )
    assert second.returncode == 0, second.stderr
    assert logged_identifiers(log) == [missing]
    assert "downloaded=0 skipped=537 failed=0" in second.stdout


def test_invalid_response_is_not_installed(tmp_path: Path) -> None:
    missing = identifiers()[-1]
    destination = tmp_path / "corpus"
    prepare_destination(destination, {missing})
    executable_directory, log = create_fake_curl(tmp_path)

    result = run_downloader(
        destination,
        executable_directory,
        log,
        FAKE_CURL_MODE="html",
    )

    assert result.returncode == 1
    assert not (destination / f"{missing}.pdf").exists()
    assert not list(destination.glob(".*.part"))
    assert "response for" in result.stderr
    assert "downloaded=0 skipped=536 failed=1" in result.stdout


def test_failure_does_not_stop_later_downloads(tmp_path: Path) -> None:
    failed_identifier, successful_identifier = identifiers()[-2:]
    destination = tmp_path / "corpus"
    prepare_destination(destination, {failed_identifier, successful_identifier})
    executable_directory, log = create_fake_curl(tmp_path)

    result = run_downloader(
        destination,
        executable_directory,
        log,
        FAKE_CURL_FAIL_ID=failed_identifier,
    )

    assert result.returncode == 1
    assert logged_identifiers(log) == [failed_identifier, successful_identifier]
    assert not (destination / f"{failed_identifier}.pdf").exists()
    assert (destination / f"{successful_identifier}.pdf").exists()
    assert not list(destination.glob(".*.part"))
    assert "downloaded=1 skipped=535 failed=1" in result.stdout


def test_interruption_removes_temporary_download(tmp_path: Path) -> None:
    missing = identifiers()[-1]
    destination = tmp_path / "corpus"
    prepare_destination(destination, {missing})
    executable_directory, log = create_fake_curl(tmp_path)
    environment = downloader_environment(
        executable_directory,
        log,
        FAKE_CURL_MODE="wait",
    )
    process = subprocess.Popen(  # noqa: S603
        [str(SCRIPT), str(destination)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not log.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert log.exists()
    os.killpg(process.pid, signal.SIGTERM)
    _stdout, _stderr = process.communicate(timeout=5)

    assert process.returncode != 0
    assert not (destination / f"{missing}.pdf").exists()
    assert not list(destination.glob(".*.part"))


def test_concurrent_destination_is_not_overwritten(tmp_path: Path) -> None:
    missing = identifiers()[-1]
    destination = tmp_path / "corpus"
    target = destination / f"{missing}.pdf"
    prepare_destination(destination, {missing})
    executable_directory, log = create_fake_curl(tmp_path)

    result = run_downloader(
        destination,
        executable_directory,
        log,
        FAKE_CURL_MODE="race",
        FAKE_CURL_RACE_TARGET=str(target),
    )

    assert result.returncode == 1
    assert target.read_bytes() == b"%PDF-existing\n"
    assert not list(destination.glob(".*.part"))
    assert "destination appeared during download" in result.stderr


def test_existing_invalid_file_is_untouched_without_network_access(tmp_path: Path) -> None:
    invalid_identifier = identifiers()[0]
    destination = tmp_path / "corpus with spaces"
    prepare_destination(destination, set())
    invalid_content = b"not a PDF"
    _ = (destination / f"{invalid_identifier}.pdf").write_bytes(invalid_content)
    executable_directory, log = create_fake_curl(tmp_path)

    result = run_downloader(
        destination,
        executable_directory,
        log,
        FAKE_CURL_MODE="fail",
    )

    assert result.returncode == 1
    assert not logged_identifiers(log)
    assert (destination / f"{invalid_identifier}.pdf").read_bytes() == invalid_content
    assert "existing path is not a valid PDF" in result.stderr
    assert "downloaded=0 skipped=536 failed=1" in result.stdout
