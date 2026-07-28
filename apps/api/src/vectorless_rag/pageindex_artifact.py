from __future__ import annotations

import gzip
import hashlib
import io
import json
from dataclasses import dataclass
from typing import Any, cast

import tiktoken
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vectorless_rag.storage import ObjectStore

ARTIFACT_SCHEMA_VERSION = 2
PAGE_SHARD_SIZE = 8
MAX_TREE_DEPTH = 64
MAX_TREE_NODES = 20_000
OUTLINE_NODE_LIMIT = 64


class ArtifactValidationError(ValueError):
    pass


class StrictArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageRange(StrictArtifactModel):
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self) -> PageRange:
        if self.page_end < self.page_start:
            raise ValueError("page range is reversed")
        return self


class PageMetric(StrictArtifactModel):
    page: int = Field(ge=1)
    text: str
    token_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    nonempty: bool


class PageShardReference(StrictArtifactModel):
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    page_count: int = Field(ge=1, le=PAGE_SHARD_SIZE)
    compressed_bytes: int = Field(ge=1)
    uncompressed_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def contiguous(self) -> PageShardReference:
        if self.page_end - self.page_start + 1 != self.page_count:
            raise ValueError("page shard bounds do not match page count")
        return self


class PageIndexNodeV2(StrictArtifactModel):
    node_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=2_000)
    direct_page_ranges: list[PageRange]
    subtree_page_range: PageRange
    direct_summary: str = Field(min_length=1, max_length=60_000)
    subtree_summary: str = Field(min_length=1, max_length=120_000)
    children: list[PageIndexNodeV2] = Field(default_factory=list)
    synthetic_kind: str | None = Field(default=None, pattern=r"^unmapped_pages$")


class OutlineNode(StrictArtifactModel):
    node_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=1_000)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)


class ArtifactQuality(BaseModel):
    # Metrics bag, not an integrity surface: the manifest digest is taken over bytes and
    # checked before parsing, so ignoring unknown keys cannot smuggle anything past it. This
    # buys forward compatibility from here on, not backwards: a reader from before these
    # counters existed still forbids extras, so rolling back to it makes every manifest
    # written since unloadable until those documents are reindexed.
    model_config = ConfigDict(extra="ignore")

    page_count: int = Field(ge=1)
    node_count: int = Field(ge=1, le=MAX_TREE_NODES)
    max_depth: int = Field(ge=0, le=MAX_TREE_DEPTH)
    covered_pages: int = Field(ge=0)
    unmapped_pages: int = Field(ge=0)
    nonempty_pages: int = Field(ge=0)
    total_page_tokens: int = Field(ge=0)
    total_page_bytes: int = Field(ge=0)
    # Per-node forward extension summed over the tree; an overflow deep in a branch
    # counts once per widened ancestor, so this is extent-of-stretching, not distinct
    # pages. Defaulted so manifests written before these counters still load.
    widened_nodes: int = Field(default=0, ge=0)
    widened_pages: int = Field(default=0, ge=0)
    # Nodes whose declared end ran before their start and were collapsed to one page. A
    # count climbing here means the generator's end-index derivation is drifting.
    reversed_ranges: int = Field(default=0, ge=0)


class PageIndexManifestV2(StrictArtifactModel):
    schema_version: int = Field(default=ARTIFACT_SCHEMA_VERSION, frozen=True)
    document_id: str = Field(min_length=1, max_length=128)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any]
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(min_length=1, max_length=60_000)
    outline: list[OutlineNode] = Field(max_length=OUTLINE_NODE_LIMIT)
    tree: list[PageIndexNodeV2] = Field(min_length=1)
    raw_object_key: str = Field(min_length=1, max_length=1024)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_shards: list[PageShardReference] = Field(min_length=1)
    generator_commit: str = Field(min_length=1, max_length=128)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=128)
    prompt_profile: str = Field(min_length=1, max_length=128)
    options_profile: dict[str, Any]
    parser: str = Field(min_length=1, max_length=64)
    parser_version: str = Field(min_length=1, max_length=64)
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality: ArtifactQuality

    @model_validator(mode="after")
    def schema_is_supported(self) -> PageIndexManifestV2:
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported artifact schema")
        return self


@dataclass(frozen=True)
class PageIndexBuildV2:
    manifest: PageIndexManifestV2
    manifest_bytes: bytes
    manifest_sha256: str
    manifest_object_key: str
    raw_bytes: bytes
    page_objects: tuple[tuple[PageShardReference, bytes], ...]


@dataclass(frozen=True)
class LoadedPageIndexV2:
    manifest: PageIndexManifestV2
    pages: dict[int, PageMetric]


@dataclass(frozen=True)
class DerivedNode:
    node: PageIndexNodeV2
    parent_id: str | None
    depth: int
    section_path: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedTree:
    tree: list[PageIndexNodeV2]
    description: str
    node_count: int
    max_depth: int
    unmapped_pages: int
    widened_nodes: int
    widened_pages: int
    reversed_ranges: int


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def configuration_digest(value: object) -> str:
    return sha256_bytes(canonical_json(value))


def deterministic_gzip(value: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        compressed.write(value)
    return buffer.getvalue()


def _raw_children(node: dict[str, Any]) -> list[dict[str, Any]]:
    value = node.get("nodes")
    if value is None:
        value = node.get("children", [])
    if not isinstance(value, list):
        raise ArtifactValidationError("node children must be a list of objects")
    children = cast(list[object], value)
    if any(not isinstance(item, dict) for item in children):
        raise ArtifactValidationError("node children must be a list of objects")
    return cast(list[dict[str, Any]], children)


def _required_text(node: dict[str, Any], key: str, fallback: str | None = None) -> str:
    raw = node.get(key, fallback)
    if not isinstance(raw, str) or not raw.strip():
        raise ArtifactValidationError(f"node {key} must not be blank")
    return raw.strip()


def _required_page(node: dict[str, Any], key: str, fallback: int | None = None) -> int:
    raw = node.get(key, fallback)
    if isinstance(raw, bool):
        raise ArtifactValidationError(f"node {key} is invalid")
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ArtifactValidationError(f"node {key} is invalid") from error
    return value


def _subtract_ranges(parent: PageRange, children: list[PageRange]) -> list[PageRange]:
    covered: set[int] = set()
    for child in children:
        covered.update(range(child.page_start, child.page_end + 1))
    direct = [page for page in range(parent.page_start, parent.page_end + 1) if page not in covered]
    if not direct:
        return []
    result: list[PageRange] = []
    start = previous = direct[0]
    for page in direct[1:]:
        if page != previous + 1:
            result.append(PageRange(page_start=start, page_end=previous))
            start = page
        previous = page
    result.append(PageRange(page_start=start, page_end=previous))
    return result


def _normalize_nodes(
    raw_nodes: list[dict[str, Any]],
    *,
    page_count: int,
    seen: set[str],
    depth: int,
) -> tuple[list[PageIndexNodeV2], int, int, int, int, int]:
    if depth > MAX_TREE_DEPTH:
        raise ArtifactValidationError("PageIndex tree exceeds maximum depth")
    normalized: list[PageIndexNodeV2] = []
    total = 0
    maximum_depth = depth
    widened_nodes = 0
    widened_pages = 0
    reversed_ranges = 0
    for raw in raw_nodes:
        node_id = _required_text(raw, "node_id")
        if node_id in seen:
            raise ArtifactValidationError(f"duplicate node ID: {node_id}")
        seen.add(node_id)
        title = _required_text(raw, "title")
        start = _required_page(raw, "start_index", _required_page(raw, "physical_index", 1))
        end = _required_page(raw, "end_index", start)
        if end < start:
            # PageIndex derives a node's end from where the *next* entry starts, so a
            # re-listed or out-of-order sibling makes the subtraction run backwards and
            # produce a range like 4-3. Only the end is manufactured that way; the start is
            # the physical index the model actually reported, so the node collapses to that
            # single page rather than discarding a whole document over an arithmetic
            # artifact. Pages its content really covers stay reachable through the unmapped
            # -pages node, which is where anything unattributed already goes.
            reversed_ranges += 1
            end = start
        if start < 1 or end > page_count:
            # Naming the range and the document length: which way a range is invalid decides
            # whether the tree is repairable, and the bare message did not say.
            raise ArtifactValidationError(
                f"node {node_id} has an invalid page range {start}-{end} "
                f"in a {page_count}-page document"
            )
        raw_children = _raw_children(raw)
        if raw_children:
            (
                child_nodes,
                child_count,
                child_depth,
                child_widened,
                child_widened_pages,
                child_reversed,
            ) = _normalize_nodes(
                raw_children,
                page_count=page_count,
                seen=seen,
                depth=depth + 1,
            )
            widened_nodes += child_widened
            widened_pages += child_widened_pages
            reversed_ranges += child_reversed
        else:
            child_nodes, child_count, child_depth = [], 0, depth
        declared_range = PageRange(page_start=start, page_end=end)
        child_ranges = [child.subtree_page_range for child in child_nodes]
        # A child that starts before its parent would annex a preceding sibling's
        # pages; sampled PageIndex output never does this, so it stays a rejection.
        if any(child.page_start < start for child in child_ranges):
            raise ArtifactValidationError(f"node {node_id} has a child before its start")
        # PageIndex regularly declares only a section heading's pages while subsections
        # run past that end_index; the v1 pipeline never enforced containment, so widen
        # the subtree range forward over the children instead of rejecting the tree.
        subtree_end = max([end, *(child.page_end for child in child_ranges)])
        subtree_range = PageRange(page_start=start, page_end=subtree_end)
        if subtree_end > end:
            widened_nodes += 1
            widened_pages += subtree_end - end
        direct_summary = _required_text(
            raw,
            "summary",
            f"{title}, pages {start} through {end}.",
        )
        child_summaries = [child.subtree_summary for child in child_nodes]
        subtree_summary = "\n".join([direct_summary, *child_summaries]).strip()
        normalized.append(
            PageIndexNodeV2(
                node_id=node_id,
                title=title,
                direct_page_ranges=_subtract_ranges(declared_range, child_ranges),
                subtree_page_range=subtree_range,
                direct_summary=direct_summary,
                subtree_summary=subtree_summary[:120_000],
                children=child_nodes,
            )
        )
        total += 1 + child_count
        maximum_depth = max(maximum_depth, child_depth)
        if total > MAX_TREE_NODES:
            raise ArtifactValidationError("PageIndex tree exceeds maximum node count")
    return normalized, total, maximum_depth, widened_nodes, widened_pages, reversed_ranges


def _unmapped_ranges(tree: list[PageIndexNodeV2], page_count: int) -> list[PageRange]:
    covered: set[int] = set()
    for node in tree:
        page_range = node.subtree_page_range
        covered.update(range(page_range.page_start, page_range.page_end + 1))
    missing = [page for page in range(1, page_count + 1) if page not in covered]
    if not missing:
        return []
    ranges: list[PageRange] = []
    start = previous = missing[0]
    for page in missing[1:]:
        if page != previous + 1:
            ranges.append(PageRange(page_start=start, page_end=previous))
            start = page
        previous = page
    ranges.append(PageRange(page_start=start, page_end=previous))
    return ranges


def normalize_tree_v2(
    raw_tree: object,
    *,
    page_count: int,
) -> NormalizedTree:
    if page_count < 1:
        raise ArtifactValidationError("PageIndex pages must not be empty")
    if isinstance(raw_tree, list):
        raw_nodes = cast(list[object], raw_tree)
        description = "A document indexed with PageIndex."
    elif isinstance(raw_tree, dict):
        raw_mapping = cast(dict[str, Any], raw_tree)
        raw_nodes_value = raw_mapping.get("structure", raw_mapping.get("nodes", []))
        if not isinstance(raw_nodes_value, list):
            raise ArtifactValidationError("PageIndex tree has no valid node list")
        raw_nodes = cast(list[object], raw_nodes_value)
        raw_description = raw_mapping.get("doc_description")
        description = (
            raw_description.strip()
            if isinstance(raw_description, str) and raw_description.strip()
            else "A document indexed with PageIndex."
        )
    else:
        raise ArtifactValidationError("PageIndex tree has an invalid top-level type")
    if not raw_nodes:
        raise ArtifactValidationError("PageIndex tree must not be empty")
    if any(not isinstance(item, dict) for item in raw_nodes):
        raise ArtifactValidationError("PageIndex roots must be objects")
    tree, node_count, max_depth, widened_nodes, widened_pages, reversed_ranges = _normalize_nodes(
        cast(list[dict[str, Any]], raw_nodes),
        page_count=page_count,
        seen=set(),
        depth=0,
    )
    unmapped = _unmapped_ranges(tree, page_count)
    for page_range in unmapped:
        node_id = f"unmapped-{page_range.page_start}-{page_range.page_end}"
        summary = (
            "Pages omitted from all generated PageIndex ranges. "
            "They remain available for evidence retrieval."
        )
        tree.append(
            PageIndexNodeV2(
                node_id=node_id,
                title="Unmapped pages",
                direct_page_ranges=[page_range],
                subtree_page_range=page_range,
                direct_summary=summary,
                subtree_summary=summary,
                synthetic_kind="unmapped_pages",
            )
        )
    node_count += len(unmapped)
    if node_count > MAX_TREE_NODES:
        raise ArtifactValidationError("PageIndex tree exceeds maximum node count")
    return NormalizedTree(
        tree=tree,
        description=description,
        node_count=node_count,
        max_depth=max_depth,
        unmapped_pages=sum(item.page_end - item.page_start + 1 for item in unmapped),
        widened_nodes=widened_nodes,
        widened_pages=widened_pages,
        reversed_ranges=reversed_ranges,
    )


def _outline(tree: list[PageIndexNodeV2]) -> list[OutlineNode]:
    return [
        OutlineNode(
            node_id=node.node_id,
            title=node.title[:500],
            summary=node.subtree_summary[:1_000],
            page_start=node.subtree_page_range.page_start,
            page_end=node.subtree_page_range.page_end,
        )
        for node in tree[:OUTLINE_NODE_LIMIT]
    ]


def _page_shards(
    pages: list[str],
) -> tuple[list[PageShardReference], tuple[tuple[PageShardReference, bytes], ...], dict[str, int]]:
    if not pages:
        raise ArtifactValidationError("PageIndex pages must not be empty")
    encoding = tiktoken.get_encoding("cl100k_base")
    references: list[PageShardReference] = []
    objects: list[tuple[PageShardReference, bytes]] = []
    totals = {"tokens": 0, "bytes": 0, "nonempty": 0}
    for offset in range(0, len(pages), PAGE_SHARD_SIZE):
        metrics: list[PageMetric] = []
        for page_index, text in enumerate(pages[offset : offset + PAGE_SHARD_SIZE], offset + 1):
            encoded = text.encode()
            # disallowed_special=() because this is a measurement of untrusted document text,
            # not a prompt being assembled. tiktoken otherwise raises on a page that merely
            # contains the literal "<|endoftext|>", which lets a PDF refuse its own indexing;
            # here those sequences are just characters to count.
            token_count = len(encoding.encode(text, disallowed_special=()))
            nonempty = bool(text.strip())
            metrics.append(
                PageMetric(
                    page=page_index,
                    text=text,
                    token_count=token_count,
                    byte_count=len(encoded),
                    nonempty=nonempty,
                )
            )
            totals["tokens"] += token_count
            totals["bytes"] += len(encoded)
            totals["nonempty"] += int(nonempty)
        raw = canonical_json([metric.model_dump(mode="json") for metric in metrics])
        compressed = deterministic_gzip(raw)
        digest = sha256_bytes(compressed)
        key = f"artifacts/v2/pages/{digest[:2]}/{digest}.json.gz"
        reference = PageShardReference(
            object_key=key,
            sha256=digest,
            page_start=metrics[0].page,
            page_end=metrics[-1].page,
            page_count=len(metrics),
            compressed_bytes=len(compressed),
            uncompressed_bytes=len(raw),
        )
        references.append(reference)
        objects.append((reference, compressed))
    return references, tuple(objects), totals


def build_manifest_v2(
    raw_tree: object,
    pages: list[str],
    *,
    document_id: str,
    document_sha256: str,
    metadata: dict[str, Any],
    generator_commit: str,
    patch_sha256: str,
    model: str,
    prompt_profile: str,
    options_profile: dict[str, Any],
    parser: str,
    parser_version: str,
) -> PageIndexBuildV2:
    raw_bytes = canonical_json(raw_tree)
    raw_sha256 = sha256_bytes(raw_bytes)
    raw_key = f"artifacts/v2/raw/{raw_sha256[:2]}/{raw_sha256}.json"
    normalized = normalize_tree_v2(raw_tree, page_count=len(pages))
    tree = normalized.tree
    description = normalized.description
    references, page_objects, totals = _page_shards(pages)
    metadata_sha256 = configuration_digest(metadata)
    configuration = {
        "generator_commit": generator_commit,
        "patch_sha256": patch_sha256,
        "model": model,
        "prompt_profile": prompt_profile,
        "options_profile": options_profile,
        "parser": parser,
        "parser_version": parser_version,
    }
    configuration_hash = configuration_digest(configuration)
    recipe_hash = configuration_digest(
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "document_sha256": document_sha256,
            "metadata_sha256": metadata_sha256,
            "configuration_hash": configuration_hash,
        }
    )
    covered_pages = len(pages) - normalized.unmapped_pages
    manifest = PageIndexManifestV2(
        document_id=document_id,
        document_sha256=document_sha256,
        metadata=metadata,
        metadata_sha256=metadata_sha256,
        description=description,
        outline=_outline(tree),
        tree=tree,
        raw_object_key=raw_key,
        raw_sha256=raw_sha256,
        page_shards=references,
        generator_commit=generator_commit,
        patch_sha256=patch_sha256,
        model=model,
        prompt_profile=prompt_profile,
        options_profile=options_profile,
        parser=parser,
        parser_version=parser_version,
        configuration_hash=configuration_hash,
        recipe_hash=recipe_hash,
        quality=ArtifactQuality(
            page_count=len(pages),
            node_count=normalized.node_count,
            max_depth=normalized.max_depth,
            covered_pages=covered_pages,
            unmapped_pages=normalized.unmapped_pages,
            nonempty_pages=totals["nonempty"],
            total_page_tokens=totals["tokens"],
            total_page_bytes=totals["bytes"],
            widened_nodes=normalized.widened_nodes,
            widened_pages=normalized.widened_pages,
            reversed_ranges=normalized.reversed_ranges,
        ),
    )
    manifest_bytes = canonical_json(manifest.model_dump(mode="json"))
    manifest_sha256 = sha256_bytes(manifest_bytes)
    manifest_key = f"artifacts/v2/manifests/{manifest_sha256[:2]}/{manifest_sha256}.json"
    return PageIndexBuildV2(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        manifest_object_key=manifest_key,
        raw_bytes=raw_bytes,
        page_objects=page_objects,
    )


async def _put_verified(
    store: ObjectStore,
    key: str,
    data: bytes,
    content_type: str,
) -> None:
    put_if_absent = getattr(store, "put_bytes_if_absent", None)
    if put_if_absent is not None:
        created = await put_if_absent(key, data, content_type)
        if created:
            return
    elif not await store.exists(key):
        await store.put_bytes(key, data, content_type)
        return
    existing = await store.get_bytes(key)
    if existing != data:
        raise ArtifactValidationError(f"content-addressed object conflict: {key}")


async def store_build_v2(store: ObjectStore, build: PageIndexBuildV2) -> None:
    await _put_verified(
        store,
        build.manifest.raw_object_key,
        build.raw_bytes,
        "application/json",
    )
    for reference, data in build.page_objects:
        await _put_verified(store, reference.object_key, data, "application/gzip")
    await _put_verified(
        store,
        build.manifest_object_key,
        build.manifest_bytes,
        "application/json",
    )


def _derive_nodes(
    nodes: list[PageIndexNodeV2],
    *,
    parent_id: str | None = None,
    depth: int = 0,
    path: tuple[str, ...] = (),
) -> list[DerivedNode]:
    result: list[DerivedNode] = []
    for node in nodes:
        section_path = (*path, node.title)
        result.append(
            DerivedNode(
                node=node,
                parent_id=parent_id,
                depth=depth,
                section_path=section_path,
            )
        )
        result.extend(
            _derive_nodes(
                node.children,
                parent_id=node.node_id,
                depth=depth + 1,
                path=section_path,
            )
        )
    return result


def derive_nodes(manifest: PageIndexManifestV2) -> list[DerivedNode]:
    return _derive_nodes(manifest.tree)


def _validate_loaded_manifest(manifest: PageIndexManifestV2) -> None:
    page_count = manifest.quality.page_count
    seen: set[str] = set()
    counted = 0
    maximum_depth = 0

    def visit(
        nodes: list[PageIndexNodeV2],
        *,
        parent_range: PageRange | None,
        depth: int,
    ) -> None:
        nonlocal counted, maximum_depth
        if depth > MAX_TREE_DEPTH:
            raise ArtifactValidationError("manifest tree exceeds maximum depth")
        for node in nodes:
            maximum_depth = max(maximum_depth, depth)
            counted += 1
            if counted > MAX_TREE_NODES:
                raise ArtifactValidationError("manifest tree exceeds maximum node count")
            if node.node_id in seen:
                raise ArtifactValidationError("manifest contains duplicate node IDs")
            seen.add(node.node_id)
            subtree = node.subtree_page_range
            if subtree.page_end > page_count:
                raise ArtifactValidationError("manifest node range exceeds page count")
            if parent_range is not None and (
                subtree.page_start < parent_range.page_start
                or subtree.page_end > parent_range.page_end
            ):
                raise ArtifactValidationError("manifest child range is outside its parent")
            for direct in node.direct_page_ranges:
                if direct.page_start < subtree.page_start or direct.page_end > subtree.page_end:
                    raise ArtifactValidationError("manifest direct range is outside its subtree")
            visit(node.children, parent_range=subtree, depth=depth + 1)

    visit(manifest.tree, parent_range=None, depth=0)
    if counted != manifest.quality.node_count:
        raise ArtifactValidationError("manifest node count does not match tree")
    if maximum_depth != manifest.quality.max_depth:
        raise ArtifactValidationError("manifest maximum depth does not match tree")
    for outline in manifest.outline:
        if outline.page_end < outline.page_start or outline.page_end > page_count:
            raise ArtifactValidationError("manifest outline range is invalid")


async def load_manifest_v2(
    store: ObjectStore,
    object_key: str,
    expected_sha256: str,
) -> LoadedPageIndexV2:
    try:
        raw_manifest = await store.get_bytes(object_key)
    except Exception as error:
        raise ArtifactValidationError("manifest object is missing") from error
    if sha256_bytes(raw_manifest) != expected_sha256:
        raise ArtifactValidationError("manifest checksum mismatch")
    try:
        manifest = PageIndexManifestV2.model_validate_json(raw_manifest)
    except ValueError as error:
        raise ArtifactValidationError("manifest validation failed") from error
    _validate_loaded_manifest(manifest)
    try:
        raw_tree = await store.get_bytes(manifest.raw_object_key)
    except Exception as error:
        raise ArtifactValidationError("raw PageIndex object is missing") from error
    if sha256_bytes(raw_tree) != manifest.raw_sha256:
        raise ArtifactValidationError("raw PageIndex checksum mismatch")
    pages: dict[int, PageMetric] = {}
    expected_page = 1
    for shard in manifest.page_shards:
        if shard.page_start != expected_page:
            raise ArtifactValidationError("page shards are missing or out of order")
        try:
            compressed = await store.get_bytes(shard.object_key)
        except Exception as error:
            raise ArtifactValidationError("page shard object is missing") from error
        if sha256_bytes(compressed) != shard.sha256:
            raise ArtifactValidationError("page shard checksum mismatch")
        if len(compressed) != shard.compressed_bytes:
            raise ArtifactValidationError("page shard compressed size mismatch")
        try:
            payload = gzip.decompress(compressed)
            if len(payload) != shard.uncompressed_bytes:
                raise ArtifactValidationError("page shard uncompressed size mismatch")
            values: object = json.loads(payload)
            if not isinstance(values, list):
                raise ValueError("page shard is not a list")
            metrics = [PageMetric.model_validate(value) for value in cast(list[object], values)]
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ArtifactValidationError("page shard is corrupt") from error
        if len(metrics) != shard.page_count:
            raise ArtifactValidationError("page shard count mismatch")
        for metric in metrics:
            if metric.page != expected_page or metric.page in pages:
                raise ArtifactValidationError("page shard has duplicate or missing pages")
            if metric.byte_count != len(metric.text.encode()):
                raise ArtifactValidationError("page byte measurement mismatch")
            if metric.nonempty != bool(metric.text.strip()):
                raise ArtifactValidationError("page nonempty measurement mismatch")
            pages[metric.page] = metric
            expected_page += 1
    if len(pages) != manifest.quality.page_count:
        raise ArtifactValidationError("manifest page count does not match shards")
    encoding = tiktoken.get_encoding("cl100k_base")
    # Must match how _page_shards measured it, or a page containing the literal
    # "<|endoftext|>" builds successfully and then fails every load.
    if any(
        metric.token_count != len(encoding.encode(metric.text, disallowed_special=()))
        for metric in pages.values()
    ):
        raise ArtifactValidationError("page token measurement mismatch")
    return LoadedPageIndexV2(manifest=manifest, pages=pages)
