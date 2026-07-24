from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import fitz

from vectorless_rag.config import Settings

PAGEINDEX_FAILURE_DIAGNOSTICS = {
    "pageindex_child_failed": "PageIndex failed inside its isolated runtime.",
    "pageindex_content_filter": (
        "The provider rejected PageIndex content under its content policy."
    ),
    "pageindex_empty_response": "The provider returned an empty PageIndex completion.",
    "pageindex_insufficient_system_resource": (
        "The provider reported insufficient system resources."
    ),
    "pageindex_invalid_json": "PageIndex received malformed JSON.",
    "pageindex_invalid_response": "PageIndex received an invalid provider response.",
    "pageindex_llm_retry_exhausted": (
        "The PageIndex provider request failed after all retry attempts."
    ),
    "pageindex_provider_rejected": "The PageIndex provider rejected the request.",
    "pageindex_transient_provider_error": ("The PageIndex provider request failed transiently."),
    "pageindex_unexpected_tool_call": "PageIndex received an unexpected tool call.",
    "usage_persistence_failed": "Model usage accounting could not be persisted.",
}


class PageIndexError(RuntimeError):
    def __init__(
        self,
        failure_code: str,
        diagnostic: str,
        *,
        attempts: int = 0,
        finish_reason: str | None = None,
    ) -> None:
        super().__init__(diagnostic)
        self.failure_code = failure_code
        self.diagnostic = diagnostic
        self.attempts = attempts
        self.finish_reason = finish_reason


class _TextPage(Protocol):
    def get_text(self, option: Literal["text"]) -> str: ...


@dataclass(frozen=True)
class ArtifactNode:
    node_id: str
    title: str
    summary: str
    page_start: int
    page_end: int


def _walk_nodes(raw_nodes: list[dict[str, Any]]) -> list[ArtifactNode]:
    result: list[ArtifactNode] = []
    for index, node in enumerate(raw_nodes):
        start = int(node.get("start_index") or node.get("physical_index") or 1)
        end = int(node.get("end_index") or start)
        result.append(
            ArtifactNode(
                node_id=str(node.get("node_id") or f"node-{index:04d}"),
                title=str(node.get("title") or "Untitled section"),
                summary=str(node.get("summary") or ""),
                page_start=max(1, start),
                page_end=max(start, end),
            )
        )
        children: object = node.get("nodes") or node.get("children") or []
        if isinstance(children, list):
            result.extend(_walk_nodes(cast(list[dict[str, Any]], children)))
    return result


def normalize_artifact(
    raw_tree: dict[str, Any] | list[dict[str, Any]], pages: list[str], *, metadata: dict[str, Any]
) -> dict[str, Any]:
    if isinstance(raw_tree, list):
        nodes = raw_tree
        description = ""
    else:
        nodes: object = raw_tree.get("structure") or raw_tree.get("nodes") or []
        description = str(raw_tree.get("doc_description") or "")
    if not isinstance(nodes, list):
        raise PageIndexError(
            "pageindex_invalid_artifact", "PageIndex artifact has no valid node list."
        )
    normalized_nodes = [node.__dict__ for node in _walk_nodes(cast(list[dict[str, Any]], nodes))]
    return {
        "schema_version": 1,
        "metadata": metadata,
        "description": description,
        "tree": normalized_nodes,
        "pages": [{"page": index + 1, "text": text} for index, text in enumerate(pages)],
    }


def extract_pdf_pages(path: Path) -> list[str]:
    with fitz.open(path) as document:
        return [cast(_TextPage, page).get_text("text") for page in document]


class PageIndexAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def index(
        self,
        pdf_path: Path,
        document_id: str,
        metadata: dict[str, Any],
        *,
        ingestion_attempt_id: UUID,
        trace_id: str,
    ) -> bytes:
        workspace_root = self.settings.pageindex_work_dir
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{document_id}-", dir=workspace_root) as temp:
            workspace = Path(temp)
            local_pdf = workspace / "document.pdf"
            await asyncio.to_thread(shutil.copyfile, pdf_path, local_pdf)
            pages = await asyncio.to_thread(extract_pdf_pages, local_pdf)
            if len(pages) == 1:
                raw_tree = [
                    {
                        "node_id": "node-0000",
                        "title": str(metadata.get("title") or "Document"),
                        "summary": str(
                            metadata.get("description") or metadata.get("abstract") or ""
                        ),
                        "start_index": 1,
                        "end_index": 1,
                    }
                ]
                artifact = normalize_artifact(raw_tree, pages, metadata=metadata)
                return json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).encode()
            command = [
                sys.executable,
                str(Path(__file__).with_name("run_pageindex.py")),
                "--pageindex-script",
                str(self.settings.pageindex_source_dir / "run_pageindex.py"),
                "--failure-report",
                str(workspace / "failure.json"),
                "--ingestion-attempt-id",
                str(ingestion_attempt_id),
                "--trace-id",
                trace_id,
                "--pdf_path",
                str(local_pdf),
                "--model",
                self.settings.pageindex_model,
                "--if-add-node-id",
                "yes",
                "--if-add-node-summary",
                "yes",
                "--if-add-doc-description",
                "yes",
                "--if-add-node-text",
                "no",
            ]
            environment = os.environ.copy()
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workspace,
                env=environment,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            waiter = asyncio.create_task(process.wait())
            try:
                async with asyncio.timeout(self.settings.pageindex_timeout_seconds):
                    await asyncio.shield(waiter)
            except TimeoutError as error:
                await asyncio.shield(self._stop_process(process, waiter))
                raise PageIndexError(
                    "pageindex_timeout",
                    "PageIndex exceeded its configured processing deadline.",
                ) from error
            except asyncio.CancelledError:
                await asyncio.shield(self._stop_process(process, waiter))
                raise
            if process.returncode:
                raise self._reported_failure(workspace / "failure.json")
            result_path = workspace / "results" / "document_structure.json"
            if not result_path.is_file():
                raise PageIndexError(
                    "pageindex_missing_artifact",
                    "PageIndex completed without producing an artifact.",
                )
            raw_tree = cast(
                dict[str, Any] | list[dict[str, Any]],
                json.loads(result_path.read_text(encoding="utf-8")),
            )
            artifact = normalize_artifact(raw_tree, pages, metadata=metadata)
            return json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).encode()

    @staticmethod
    async def _stop_process(
        process: asyncio.subprocess.Process,
        waiter: asyncio.Task[int],
    ) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            async with asyncio.timeout(5):
                await asyncio.shield(waiter)
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await waiter

    @staticmethod
    def _reported_failure(path: Path) -> PageIndexError:
        try:
            if path.stat().st_size > 16_384:
                raise ValueError("failure report exceeds limit")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("failure report is not an object")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return PageIndexError(
                "pageindex_child_failed", "PageIndex failed inside its isolated runtime."
            )
        report = cast(dict[str, object], raw)
        code = report.get("failure_code")
        finish_reason = report.get("finish_reason")
        attempts = report.get("llm_attempts", 0)
        if not isinstance(code, str) or code not in PAGEINDEX_FAILURE_DIAGNOSTICS:
            return PageIndexError(
                "pageindex_child_failed", "PageIndex failed inside its isolated runtime."
            )
        return PageIndexError(
            code,
            PAGEINDEX_FAILURE_DIAGNOSTICS[code],
            attempts=(
                attempts
                if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0
                else 0
            ),
            finish_reason=finish_reason[:80] if isinstance(finish_reason, str) else None,
        )


def artifact_version_key(
    pdf_sha256: str, pageindex_version: str, prompt_version: str, model_name: str
) -> str:
    value = "\0".join((pdf_sha256, pageindex_version, prompt_version, model_name))
    return hashlib.sha256(value.encode()).hexdigest()
