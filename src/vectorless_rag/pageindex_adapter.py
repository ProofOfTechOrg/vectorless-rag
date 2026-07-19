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

import fitz

from vectorless_rag.config import Settings


class PageIndexError(RuntimeError):
    pass


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
        raise PageIndexError("PageIndex artifact has no valid node list")
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

    async def index(self, pdf_path: Path, document_id: str, metadata: dict[str, Any]) -> bytes:
        workspace_root = self.settings.pageindex_work_dir
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{document_id}-", dir=workspace_root) as temp:
            workspace = Path(temp)
            local_pdf = workspace / "document.pdf"
            await asyncio.to_thread(shutil.copyfile, pdf_path, local_pdf)
            command = [
                sys.executable,
                str(Path(__file__).with_name("run_pageindex.py")),
                "--pageindex-script",
                str(self.settings.pageindex_source_dir / "run_pageindex.py"),
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
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode:
                diagnostic = stderr.decode(errors="replace")[-4000:]
                raise PageIndexError(f"PageIndex failed: {diagnostic}")
            result_path = workspace / "results" / "document_structure.json"
            if not result_path.is_file():
                raise PageIndexError(
                    f"PageIndex produced no artifact: {stdout.decode(errors='replace')[-1000:]}"
                )
            raw_tree = cast(
                dict[str, Any] | list[dict[str, Any]],
                json.loads(result_path.read_text(encoding="utf-8")),
            )
            pages = await asyncio.to_thread(extract_pdf_pages, local_pdf)
            artifact = normalize_artifact(raw_tree, pages, metadata=metadata)
            return json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).encode()


def artifact_version_key(
    pdf_sha256: str, pageindex_version: str, prompt_version: str, model_name: str
) -> str:
    value = "\0".join((pdf_sha256, pageindex_version, prompt_version, model_name))
    return hashlib.sha256(value.encode()).hexdigest()
