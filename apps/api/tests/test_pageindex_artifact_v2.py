from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from vectorless_rag.pageindex_artifact import (
    ArtifactValidationError,
    PageIndexBuildV2,
    build_manifest_v2,
    derive_nodes,
    load_manifest_v2,
    normalize_tree_v2,
    sha256_bytes,
    store_build_v2,
)
from vectorless_rag.storage import MemoryObjectStore


def _tree() -> dict[str, Any]:
    return {
        "doc_description": "A nested test document.",
        "structure": [
            {
                "node_id": "root",
                "title": "Root",
                "start_index": 2,
                "end_index": 8,
                "summary": "Root direct summary.",
                "nodes": [
                    {
                        "node_id": "child",
                        "title": "Child",
                        "start_index": 3,
                        "end_index": 4,
                        "summary": "Child direct summary.",
                    }
                ],
            }
        ],
    }


def _build(*, prompt_profile: str = "pageindex-v2") -> PageIndexBuildV2:
    return build_manifest_v2(
        _tree(),
        [f"page {page}" for page in range(1, 11)],
        document_id="document-1",
        document_sha256="a" * 64,
        metadata={"title": "Fixture"},
        generator_commit="190f8b",
        patch_sha256="b" * 64,
        model="provider/model",
        prompt_profile=prompt_profile,
        options_profile={"toc": "auto"},
        parser="pymupdf",
        parser_version="1",
    )


async def test_nested_manifest_is_deterministic_sharded_and_loadable() -> None:
    first = _build()
    second = _build()
    assert first.manifest_bytes == second.manifest_bytes
    assert first.page_objects == second.page_objects
    assert len(first.page_objects) == 2
    assert first.manifest.quality.node_count == 4
    assert first.manifest.quality.max_depth == 1
    assert first.manifest.quality.unmapped_pages == 3
    assert [
        node.node.synthetic_kind
        for node in derive_nodes(first.manifest)
        if node.node.synthetic_kind
    ] == ["unmapped_pages", "unmapped_pages"]

    store = MemoryObjectStore()
    await store_build_v2(store, first)
    loaded = await load_manifest_v2(
        store,
        first.manifest_object_key,
        first.manifest_sha256,
    )
    assert list(loaded.pages) == list(range(1, 11))
    assert loaded.pages[10].text == "page 10"


def test_recipe_hash_changes_when_prompt_profile_changes() -> None:
    first = _build(prompt_profile="profile-a")
    second = _build(prompt_profile="profile-b")
    assert first.manifest.recipe_hash != second.manifest.recipe_hash
    assert first.manifest.configuration_hash != second.manifest.configuration_hash


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "blank",
        "reversed",
        "out_of_bounds",
        "duplicate",
        "child_outside",
    ],
)
def test_invalid_tree_shapes_are_rejected(mutation: str) -> None:
    tree = copy.deepcopy(_tree())
    root = tree["structure"][0]
    if mutation == "empty":
        tree["structure"] = []
    elif mutation == "blank":
        root["title"] = " "
    elif mutation == "reversed":
        root.update({"start_index": 9, "end_index": 2})
    elif mutation == "out_of_bounds":
        root["end_index"] = 11
    elif mutation == "duplicate":
        root["nodes"][0]["node_id"] = "root"
    elif mutation == "child_outside":
        root["nodes"][0].update({"start_index": 1, "end_index": 4})
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    with pytest.raises(ArtifactValidationError):
        normalize_tree_v2(tree, page_count=10)


async def test_content_addressed_conflict_is_rejected() -> None:
    build = _build()
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    store.objects[build.manifest.raw_object_key] = b"conflict"
    with pytest.raises(ArtifactValidationError, match="content-addressed object conflict"):
        await store_build_v2(store, build)


async def test_missing_and_corrupt_shards_are_rejected() -> None:
    build = _build()
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    first_shard = build.manifest.page_shards[0]
    del store.objects[first_shard.object_key]
    with pytest.raises(ArtifactValidationError, match="page shard object is missing"):
        await load_manifest_v2(
            store,
            build.manifest_object_key,
            build.manifest_sha256,
        )

    await store_build_v2(store, build)
    store.objects[first_shard.object_key] = b"not-gzip"
    with pytest.raises(ArtifactValidationError, match="checksum mismatch"):
        await load_manifest_v2(
            store,
            build.manifest_object_key,
            build.manifest_sha256,
        )


async def test_loader_rejects_extra_fields_and_unsupported_schema() -> None:
    build = _build()
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    raw = json.loads(build.manifest_bytes)
    raw["unexpected"] = True
    invalid = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    key = "artifacts/v2/manifests/invalid.json"
    store.objects[key] = invalid
    with pytest.raises(ArtifactValidationError, match="manifest validation failed"):
        await load_manifest_v2(store, key, sha256_bytes(invalid))

    raw.pop("unexpected")
    raw["schema_version"] = 3
    invalid = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    store.objects[key] = invalid
    with pytest.raises(ArtifactValidationError, match="manifest validation failed"):
        await load_manifest_v2(store, key, sha256_bytes(invalid))
