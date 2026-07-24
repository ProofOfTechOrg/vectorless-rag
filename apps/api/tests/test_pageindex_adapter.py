import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from vectorless_rag.config import Settings
from vectorless_rag.pageindex_adapter import (
    PageIndexAdapter,
    PageIndexError,
    artifact_version_key,
    normalize_artifact,
)
from vectorless_rag.run_pageindex import build_failure_report

_ATTEMPT_ID = uuid.uuid4()
_TRACE_ID = uuid.uuid4().hex


async def _index(
    adapter: PageIndexAdapter,
    source: Path,
    document_id: str,
    metadata: dict[str, Any],
) -> bytes:
    return await adapter.index(
        source,
        document_id,
        metadata,
        ingestion_attempt_id=_ATTEMPT_ID,
        trace_id=_TRACE_ID,
    )


class _Process:
    def __init__(
        self,
        returncode: int | None,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        complete_on_terminate: bool = True,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.complete_on_terminate = complete_on_terminate
        self.started = asyncio.Event()
        self.completed = asyncio.Event()
        self.terminated = False
        self.killed = False
        self.waited = False
        if returncode is not None:
            self.completed.set()

    async def wait(self) -> int:
        self.started.set()
        if self.returncode is None:
            await self.completed.wait()
        self.waited = True
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.complete_on_terminate:
            self.returncode = -15
            self.completed.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.completed.set()


def _prepare_multi_page(monkeypatch: MonkeyPatch) -> None:
    async def inline_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    def extract_pages(path: Path) -> list[str]:
        del path
        return ["one", "two"]

    monkeypatch.setattr("vectorless_rag.pageindex_adapter.asyncio.to_thread", inline_to_thread)
    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.extract_pdf_pages",
        extract_pages,
    )


def test_artifact_normalizes_nested_tree_and_pages() -> None:
    raw = {
        "doc_description": "A paper",
        "structure": [
            {
                "node_id": "0001",
                "title": "Methods",
                "start_index": 2,
                "end_index": 4,
                "summary": "Method details",
                "nodes": [{"node_id": "0002", "title": "Data", "start_index": 3}],
            }
        ],
    }
    artifact = normalize_artifact(raw, ["one", "two", "three", "four"], metadata={"title": "T"})
    assert artifact["schema_version"] == 1
    assert [node["node_id"] for node in artifact["tree"]] == ["0001", "0002"]
    assert artifact["tree"][1]["page_start"] == 3
    assert artifact["pages"][0] == {"page": 1, "text": "one"}


def test_index_version_changes_for_every_material_input() -> None:
    base = artifact_version_key("sha", "pageindex", "prompt", "model")
    assert base != artifact_version_key("other", "pageindex", "prompt", "model")
    assert base != artifact_version_key("sha", "pageindex-2", "prompt", "model")
    assert base != artifact_version_key("sha", "pageindex", "prompt-2", "model")
    assert base != artifact_version_key("sha", "pageindex", "prompt", "model-2")


def test_child_failure_report_maps_codes_to_fixed_safe_diagnostics() -> None:
    error = PageIndexError(
        "pageindex_content_filter",
        "private document fragment",
        attempts=2,
        finish_reason="content_filter",
    )

    assert build_failure_report(error) == {
        "failure_code": "pageindex_content_filter",
        "diagnostic": "The provider rejected PageIndex content under its content policy.",
        "finish_reason": "content_filter",
        "llm_attempts": 2,
    }
    assert build_failure_report(RuntimeError("private provider body")) == {
        "failure_code": "pageindex_child_failed",
        "diagnostic": "PageIndex failed inside its isolated runtime.",
    }


async def test_single_page_builds_deterministic_artifact_without_pageindex(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")

    async def inline_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("vectorless_rag.pageindex_adapter.asyncio.to_thread", inline_to_thread)

    def extracted_pages(path: Path) -> list[str]:
        del path
        return ["unique one-page evidence"]

    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.extract_pdf_pages",
        extracted_pages,
    )

    async def fail_subprocess(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("single-page indexing must not launch PageIndex")

    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.asyncio.create_subprocess_exec",
        fail_subprocess,
    )
    adapter = PageIndexAdapter(Settings(pageindex_work_dir=tmp_path / "work"))

    raw = await _index(
        adapter,
        source,
        "document-id",
        {"title": "One page", "description": "A verification document"},
    )
    artifact = json.loads(raw)

    assert artifact["tree"] == [
        {
            "node_id": "node-0000",
            "title": "One page",
            "summary": "A verification document",
            "page_start": 1,
            "page_end": 1,
        }
    ]
    assert artifact["pages"] == [{"page": 1, "text": "unique one-page evidence"}]


async def test_subprocess_success_reads_only_the_artifact(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_multi_page(monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")
    process = _Process(0, stdout=b"private document output", stderr=b"private error output")

    async def create_process(*args: object, **kwargs: object) -> _Process:
        command = [str(value) for value in args]
        assert command[command.index("--ingestion-attempt-id") + 1] == str(_ATTEMPT_ID)
        assert command[command.index("--trace-id") + 1] == _TRACE_ID
        assert kwargs["stdout"] is asyncio.subprocess.DEVNULL
        assert kwargs["stderr"] is asyncio.subprocess.DEVNULL
        workspace = Path(str(kwargs["cwd"]))
        result = workspace / "results" / "document_structure.json"
        result.parent.mkdir()
        result.write_text('[{"title":"Section","start_index":1}]', encoding="utf-8")
        return process

    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.asyncio.create_subprocess_exec", create_process
    )
    adapter = PageIndexAdapter(Settings(pageindex_work_dir=tmp_path / "work"))

    artifact = json.loads(await _index(adapter, source, "document-id", {"title": "Paper"}))

    assert artifact["tree"][0]["title"] == "Section"
    assert process.waited


async def test_failure_report_is_allowlisted_and_raw_pipes_are_never_exposed(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_multi_page(monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")
    private_text = "private-document-fragment"
    process = _Process(1, stdout=private_text.encode(), stderr=f"api-key {private_text}".encode())

    async def create_process(*args: object, **kwargs: object) -> _Process:
        del args
        workspace = Path(str(kwargs["cwd"]))
        (workspace / "failure.json").write_text(
            json.dumps(
                {
                    "failure_code": "pageindex_content_filter",
                    "diagnostic": private_text,
                    "finish_reason": "content_filter",
                    "llm_attempts": 1,
                    "raw": private_text,
                }
            ),
            encoding="utf-8",
        )
        return process

    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.asyncio.create_subprocess_exec", create_process
    )
    adapter = PageIndexAdapter(Settings(pageindex_work_dir=tmp_path / "work"))

    with pytest.raises(PageIndexError) as error:
        await _index(adapter, source, "document-id", {"title": "Paper"})

    assert error.value.failure_code == "pageindex_content_filter"
    assert error.value.finish_reason == "content_filter"
    assert error.value.attempts == 1
    assert private_text not in str(error.value)
    assert process.waited


async def test_missing_failure_report_and_artifact_use_safe_diagnostics(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_multi_page(monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")
    processes = iter([_Process(1, stderr=b"private raw failure"), _Process(0)])

    async def create_process(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        return next(processes)

    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.asyncio.create_subprocess_exec", create_process
    )
    adapter = PageIndexAdapter(Settings(pageindex_work_dir=tmp_path / "work"))

    with pytest.raises(PageIndexError, match="isolated runtime") as failed:
        await _index(adapter, source, "first", {"title": "Paper"})
    assert failed.value.failure_code == "pageindex_child_failed"
    with pytest.raises(PageIndexError, match="without producing") as missing:
        await _index(adapter, source, "second", {"title": "Paper"})
    assert missing.value.failure_code == "pageindex_missing_artifact"


class _ImmediateTimeout:
    async def __aenter__(self) -> None:
        raise TimeoutError

    async def __aexit__(self, *args: object) -> None:
        del args


async def test_timeout_terminates_then_forces_kill_and_reaps(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_multi_page(monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")
    process = _Process(None, complete_on_terminate=False)

    async def create_process(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        return process

    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.asyncio.create_subprocess_exec", create_process
    )

    def immediate_timeout(seconds: float) -> _ImmediateTimeout:
        del seconds
        return _ImmediateTimeout()

    monkeypatch.setattr("vectorless_rag.pageindex_adapter.asyncio.timeout", immediate_timeout)
    adapter = PageIndexAdapter(Settings(pageindex_work_dir=tmp_path / "work"))

    with pytest.raises(PageIndexError) as error:
        await _index(adapter, source, "document-id", {"title": "Paper"})

    assert error.value.failure_code == "pageindex_timeout"
    assert process.terminated
    assert process.killed
    assert process.waited


async def test_cancellation_terminates_and_reaps_before_propagating(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_multi_page(monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")
    process = _Process(None)

    async def create_process(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        return process

    monkeypatch.setattr(
        "vectorless_rag.pageindex_adapter.asyncio.create_subprocess_exec", create_process
    )
    adapter = PageIndexAdapter(Settings(pageindex_work_dir=tmp_path / "work"))
    task = asyncio.create_task(_index(adapter, source, "document-id", {"title": "Paper"}))
    await process.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated
    assert process.waited
    assert not process.killed
