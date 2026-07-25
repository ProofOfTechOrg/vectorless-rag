from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import fitz

from vectorless_rag.config import Settings
from vectorless_rag.pageindex_artifact import (
    PageIndexBuildV2,
    build_manifest_v2,
)
from vectorless_rag.pageindex_failures import PAGEINDEX_FAILURE_DIAGNOSTICS


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

    def _child_environment(self) -> dict[str, str]:
        environment = {
            "DATABASE_URL": self.settings.database_url,
            "DEEPSEEK_MODEL": self.settings.deepseek_model,
            "PAGEINDEX_MODEL": self.settings.pageindex_model,
            "PAGEINDEX_LLM_MAX_ATTEMPTS": str(self.settings.pageindex_llm_max_attempts),
            "PAGEINDEX_ASYNC_CONCURRENCY": str(self.settings.pageindex_async_concurrency),
            "PAGEINDEX_MAX_OUTPUT_TOKENS": str(self.settings.pageindex_max_output_tokens),
            "PAGEINDEX_CALL_TIMEOUT_SECONDS": str(self.settings.pageindex_call_timeout_seconds),
            "RELEASE": self.settings.release,
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        if self.settings.deepseek_api_key is not None:
            environment["DEEPSEEK_API_KEY"] = self.settings.deepseek_api_key.get_secret_value()
        for name in (
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        ):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment

    async def build_v2(
        self,
        pdf_path: Path,
        document_id: str,
        document_sha256: str,
        metadata: dict[str, Any],
        *,
        ingestion_attempt_id: UUID,
        trace_id: str,
    ) -> PageIndexBuildV2:
        workspace_root = self.settings.pageindex_work_dir
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{document_id}-", dir=workspace_root) as temp:
            workspace = Path(temp)
            local_pdf = workspace / "document.pdf"
            await asyncio.to_thread(shutil.copyfile, pdf_path, local_pdf)
            preliminary_pages = await asyncio.to_thread(extract_pdf_pages, local_pdf)
            if len(preliminary_pages) == 1:
                title = str(metadata.get("title") or "Document").strip() or "Document"
                summary = str(
                    metadata.get("description") or metadata.get("abstract") or title
                ).strip()
                raw_tree: object = {
                    "doc_description": summary,
                    "structure": [
                        {
                            "node_id": "node-0000",
                            "title": title,
                            "summary": summary,
                            "start_index": 1,
                            "end_index": 1,
                        }
                    ],
                }
                pages = preliminary_pages
            else:
                raw_tree, pages = await self._run_v2_child(
                    workspace,
                    local_pdf,
                    ingestion_attempt_id=ingestion_attempt_id,
                    trace_id=trace_id,
                )
            patch_path = Path(__file__).parents[2] / "patches" / "pageindex-runtime.patch"
            patch_sha256 = hashlib.sha256(patch_path.read_bytes()).hexdigest()
            return build_manifest_v2(
                raw_tree,
                pages,
                document_id=document_id,
                document_sha256=document_sha256,
                metadata=metadata,
                generator_commit=self.settings.pageindex_version.split("+", 1)[0],
                patch_sha256=patch_sha256,
                model=self.settings.pageindex_model,
                prompt_profile="pageindex-v2-untrusted-document-v1",
                options_profile={
                    "add_node_id": True,
                    "add_node_summary": True,
                    "add_doc_description": True,
                    "add_node_text": False,
                },
                parser="PyPDF2",
                parser_version=importlib.metadata.version("PyPDF2"),
            )

    async def _run_v2_child(
        self,
        workspace: Path,
        local_pdf: Path,
        *,
        ingestion_attempt_id: UUID,
        trace_id: str,
    ) -> tuple[object, list[str]]:
        pages_output = workspace / "document_pages.json"
        command = self._command(
            workspace,
            local_pdf,
            ingestion_attempt_id=ingestion_attempt_id,
            trace_id=trace_id,
            pages_output=pages_output,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workspace,
            env=self._child_environment(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await self._wait_for_child(process, workspace)
        result_path = workspace / "results" / "document_structure.json"
        if not result_path.is_file():
            raise PageIndexError(
                "pageindex_missing_artifact",
                "PageIndex completed without producing an artifact.",
            )
        if not pages_output.is_file():
            raise PageIndexError(
                "pageindex_missing_pages",
                "PageIndex completed without producing its exact page text.",
            )
        try:
            raw_tree: object = json.loads(result_path.read_text(encoding="utf-8"))
            raw_pages: object = json.loads(pages_output.read_text(encoding="utf-8"))
            if not isinstance(raw_pages, list):
                raise ValueError("invalid page output")
            page_values = cast(list[object], raw_pages)
            if any(not isinstance(page, str) for page in page_values):
                raise ValueError("invalid page output")
            pages = cast(list[str], page_values)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise PageIndexError(
                "pageindex_invalid_artifact",
                "PageIndex produced an invalid artifact.",
            ) from error
        return raw_tree, pages

    def _command(
        self,
        workspace: Path,
        local_pdf: Path,
        *,
        ingestion_attempt_id: UUID,
        trace_id: str,
        pages_output: Path | None = None,
    ) -> list[str]:
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
        ]
        if pages_output is not None:
            command.extend(["--pages-output", str(pages_output)])
        command.extend(
            [
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
        )
        return command

    async def _wait_for_child(self, process: asyncio.subprocess.Process, workspace: Path) -> None:
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
            command = self._command(
                workspace,
                local_pdf,
                ingestion_attempt_id=ingestion_attempt_id,
                trace_id=trace_id,
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workspace,
                env=self._child_environment(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await self._wait_for_child(process, workspace)
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
                process_id = getattr(process, "pid", None)
                if isinstance(process_id, int):
                    os.killpg(process_id, signal.SIGTERM)
                else:
                    process.terminate()
            except (ProcessLookupError, PermissionError):
                pass
        try:
            async with asyncio.timeout(5):
                await asyncio.shield(waiter)
        except TimeoutError:
            if process.returncode is None:
                try:
                    process_id = getattr(process, "pid", None)
                    if isinstance(process_id, int):
                        os.killpg(process_id, signal.SIGKILL)
                    else:
                        process.kill()
                except (ProcessLookupError, PermissionError):
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
