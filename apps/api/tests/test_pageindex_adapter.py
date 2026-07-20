import json
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from vectorless_rag.config import Settings
from vectorless_rag.pageindex_adapter import (
    PageIndexAdapter,
    artifact_version_key,
    normalize_artifact,
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

    raw = await adapter.index(
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
