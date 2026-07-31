from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from vectorless_rag.pageindex_artifact import (
    ARTIFACT_BUILDER_VERSION,
    ArtifactValidationError,
    PageIndexBuildV2,
    PageRange,
    build_manifest_v2,
    configuration_digest,
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


def _build(
    *, tree: dict[str, Any] | None = None, prompt_profile: str = "pageindex-v2"
) -> PageIndexBuildV2:
    return build_manifest_v2(
        tree if tree is not None else _tree(),
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


def test_artifact_builder_implementation_is_part_of_recipe_identity() -> None:
    build = _build()

    assert build.manifest.configuration_hash == configuration_digest(
        {
            "artifact_builder_version": ARTIFACT_BUILDER_VERSION,
            "generator_commit": "190f8b",
            "patch_sha256": "b" * 64,
            "model": "provider/model",
            "prompt_profile": "pageindex-v2",
            "options_profile": {"toc": "auto"},
            "parser": "pymupdf",
            "parser_version": "1",
        }
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "blank",
        "reversed",
        "out_of_bounds",
        "duplicate",
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
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    with pytest.raises(ArtifactValidationError):
        normalize_tree_v2(tree, page_count=10)


async def test_child_overflow_widens_ancestor_subtree_ranges_and_loads() -> None:
    # PageIndex assigns a section its heading's range; descendants regularly run past
    # it, so normalization must widen ancestors forward rather than reject the tree.
    tree = copy.deepcopy(_tree())
    root = tree["structure"][0]
    root["nodes"][0]["nodes"] = [
        {
            "node_id": "grandchild",
            "title": "Grandchild",
            "start_index": 4,
            "end_index": 9,
            "summary": "Grandchild direct summary.",
        }
    ]
    normalized = normalize_tree_v2(tree, page_count=10)
    root_node = normalized.tree[0]
    child = root_node.children[0]
    grandchild = child.children[0]
    assert grandchild.subtree_page_range == PageRange(page_start=4, page_end=9)
    assert child.subtree_page_range == PageRange(page_start=3, page_end=9)
    assert root_node.subtree_page_range == PageRange(page_start=2, page_end=9)
    # Direct ranges stay anchored to each node's declared range minus its children.
    assert root_node.direct_page_ranges == [PageRange(page_start=2, page_end=2)]
    assert child.direct_page_ranges == [PageRange(page_start=3, page_end=3)]
    assert grandchild.direct_page_ranges == [PageRange(page_start=4, page_end=9)]
    assert normalized.node_count == 5
    assert normalized.max_depth == 2
    assert normalized.unmapped_pages == 2
    assert (normalized.widened_nodes, normalized.widened_pages) == (2, 6)  # child 4->9, root 8->9

    build = _build(tree=tree)
    assert build.manifest.quality.widened_nodes == 2
    assert build.manifest.quality.widened_pages == 6
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    loaded = await load_manifest_v2(
        store,
        build.manifest_object_key,
        build.manifest_sha256,
    )
    assert loaded.manifest.tree[0].subtree_page_range == PageRange(page_start=2, page_end=9)


def test_backward_child_underflow_is_rejected() -> None:
    # A child starting before its parent would annex a preceding sibling's pages;
    # sampled PageIndex output only overflows forward, so this stays malformed.
    tree = copy.deepcopy(_tree())
    tree["structure"][0]["nodes"][0].update({"start_index": 1, "end_index": 4})
    with pytest.raises(ArtifactValidationError, match="child before its start"):
        normalize_tree_v2(tree, page_count=10)


async def test_gap_between_declared_range_and_child_stays_inside_subtree() -> None:
    # Declared 2-3 with a child at 7-9: the widened subtree bridges the gap for retrieval,
    # but pages 4-6 still belong to no generated node and remain explicitly discoverable.
    tree = copy.deepcopy(_tree())
    root = tree["structure"][0]
    root.update({"start_index": 2, "end_index": 3})
    root["nodes"][0].update({"start_index": 7, "end_index": 9})
    normalized = normalize_tree_v2(tree, page_count=10)
    root_node = normalized.tree[0]
    assert root_node.subtree_page_range == PageRange(page_start=2, page_end=9)
    assert root_node.direct_page_ranges == [PageRange(page_start=2, page_end=3)]
    assert root_node.children[0].direct_page_ranges == [PageRange(page_start=7, page_end=9)]
    assert normalized.unmapped_pages == 5  # pages 1, 4-6, and 10
    assert normalized.node_count == 5  # two real nodes plus three synthetic unmapped ranges
    assert (normalized.widened_nodes, normalized.widened_pages) == (1, 6)

    build = _build(tree=tree)
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    await load_manifest_v2(store, build.manifest_object_key, build.manifest_sha256)


async def test_overlapping_root_subtrees_are_accepted() -> None:
    # Adjacent sections sharing boundary pages is normal PageIndex output; sibling
    # windows may overlap after widening and the loader accepts them.
    tree = {
        "doc_description": "Overlapping roots.",
        "structure": [
            {
                "node_id": "a",
                "title": "A",
                "start_index": 1,
                "end_index": 4,
                "summary": "A summary.",
                "nodes": [
                    {
                        "node_id": "a1",
                        "title": "A1",
                        "start_index": 3,
                        "end_index": 6,
                        "summary": "A1 summary.",
                    }
                ],
            },
            {
                "node_id": "b",
                "title": "B",
                "start_index": 5,
                "end_index": 10,
                "summary": "B summary.",
            },
        ],
    }
    normalized = normalize_tree_v2(tree, page_count=10)
    assert normalized.tree[0].subtree_page_range == PageRange(page_start=1, page_end=6)
    assert normalized.tree[1].subtree_page_range == PageRange(page_start=5, page_end=10)
    assert normalized.unmapped_pages == 0
    assert normalized.widened_nodes == 1

    build = _build(tree=tree)
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    await load_manifest_v2(store, build.manifest_object_key, build.manifest_sha256)


def test_contained_tree_normalizes_without_widening() -> None:
    normalized = normalize_tree_v2(_tree(), page_count=10)
    assert normalized.tree[0].subtree_page_range == PageRange(page_start=2, page_end=8)
    assert (normalized.widened_nodes, normalized.widened_pages) == (0, 0)


async def test_loader_rejects_child_outside_parent_subtree() -> None:
    # The builder can no longer produce this shape; the loader boundary still must
    # reject hand-forged or legacy manifests that carry it.
    build = _build()
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    raw = json.loads(build.manifest_bytes)
    raw["tree"][0]["children"][0]["subtree_page_range"] = {"page_start": 3, "page_end": 9}
    forged = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    key = "artifacts/v2/manifests/forged.json"
    store.objects[key] = forged
    with pytest.raises(ArtifactValidationError, match="outside its parent"):
        await load_manifest_v2(store, key, sha256_bytes(forged))


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


def test_a_reversed_child_range_collapses_instead_of_failing_the_document() -> None:
    # PageIndex derives a node's end from where the next entry starts, so an out-of-order
    # sibling makes that subtraction run backwards. Losing a whole verified document to the
    # arithmetic is a worse answer than trusting the start the model actually reported.
    tree = _tree()
    tree["structure"][0]["nodes"][0].update({"start_index": 4, "end_index": 3})

    normalized = normalize_tree_v2(tree, page_count=10)

    child = normalized.tree[0].children[0]
    assert child.subtree_page_range == PageRange(page_start=4, page_end=4)
    assert child.direct_page_ranges == [PageRange(page_start=4, page_end=4)]
    assert normalized.reversed_ranges == 1


def test_a_range_running_past_the_document_is_still_refused() -> None:
    # Only the end is manufactured by the derivation. A start or end outside the document is
    # a claim about pages that do not exist, which stays fatal.
    tree = _tree()
    tree["structure"][0]["nodes"][0].update({"start_index": 3, "end_index": 99})

    with pytest.raises(ArtifactValidationError, match="invalid page range 3-99"):
        normalize_tree_v2(tree, page_count=10)


def test_a_page_containing_a_special_token_literal_still_indexes() -> None:
    # tiktoken refuses by default on text merely containing "<|endoftext|>", so a PDF
    # carrying that string could deny its own indexing. It is data here, not a control token.
    pages = ["Ordinary first page.", "A page quoting <|endoftext|> in its body."]
    pages += [f"Filler page {number}." for number in range(3, 11)]

    build = build_manifest_v2(
        document_id="doc",
        document_sha256="0" * 64,
        metadata={"title": "Special tokens"},
        raw_tree=_tree(),
        pages=pages,
        generator_commit="commit",
        patch_sha256="1" * 64,
        model="model",
        prompt_profile="profile",
        options_profile={},
        parser="pypdf",
        parser_version="6.14.2",
    )

    assert build.manifest.quality.page_count == 10
    assert build.manifest.quality.total_page_tokens > 0
