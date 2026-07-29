from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import tiktoken
from sqlalchemy import Date, Text, and_, case, func, literal, or_, select, tuple_
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings
from vectorless_rag.llm import UNTRUSTED_DOCUMENT_SYSTEM, StructuredLLM, wrap_untrusted_document
from vectorless_rag.models import (
    Document,
    DocumentMetadataVersion,
    DocumentStatus,
    IndexVersion,
    ModelCallStage,
)
from vectorless_rag.pageindex_artifact import (
    DerivedNode,
    derive_nodes,
    load_manifest_v2,
)
from vectorless_rag.schemas import (
    CandidateSelection,
    DocumentMetadata,
    Evidence,
    MetadataConstraints,
    NodeChoice,
    NodeSelection,
    SufficiencyDecision,
)
from vectorless_rag.storage import ObjectStore

QUOTED_PHRASE = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
FALLBACK_SEARCH_EXCLUDED_TOKENS = frozenset({"or", "and", "not"})
MODEL_TEXT_TOKEN_LIMIT = 2_048
COMPARISON_TEXT_TOKEN_FLOOR = 8
COMPARISON_TEXT_TOKEN_LIMIT = 256
CATALOG_MAP_DETAIL_LIMIT = 256
CATALOG_MAP_OUTLINE_LIMIT = 16


async def _gather_or_cancel[ResultT](
    coroutines: Sequence[Coroutine[Any, Any, ResultT]],
) -> list[ResultT]:
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _token_count(encoding: tiktoken.Encoding, value: object) -> int:
    text = value if isinstance(value, str) else _canonical_json(value)
    # disallowed_special=() throughout this module: these count and slice untrusted
    # document text, and tiktoken otherwise raises on a page that merely contains the
    # literal "<|endoftext|>". Special sequences are characters here, never controls.
    return len(encoding.encode(text, disallowed_special=()))


def _wrapped_token_count(encoding: tiktoken.Encoding, value: object) -> int:
    text = value if isinstance(value, str) else _canonical_json(value)
    return len(encoding.encode(wrap_untrusted_document(text), disallowed_special=()))


def _truncate_text(
    encoding: tiktoken.Encoding,
    value: str,
    limit: int,
    cache: dict[str, list[int]],
) -> str:
    if not value or limit <= 0:
        return ""
    tokens = cache.get(value)
    if tokens is None:
        character_limit = min(len(value), MODEL_TEXT_TOKEN_LIMIT * 16)
        while True:
            tokens = encoding.encode(value[:character_limit], disallowed_special=())
            if len(tokens) >= MODEL_TEXT_TOKEN_LIMIT or character_limit == len(value):
                break
            character_limit = min(len(value), character_limit * 2)
        tokens = tokens[:MODEL_TEXT_TOKEN_LIMIT]
        cache[value] = tokens
    return encoding.decode(tokens[:limit])


def candidate_search_text(question: str) -> str:
    phrases = [next(value for value in match if value) for match in QUOTED_PHRASE.findall(question)]
    return max(phrases, key=len) if phrases else question


def fallback_search_tokens(question: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", question.lower())
    return list(
        dict.fromkeys(token for token in tokens if token not in FALLBACK_SEARCH_EXCLUDED_TOKENS)
    )[:24]


@dataclass(frozen=True)
class CandidateDocument:
    document: Document
    fts_match: bool = False
    lexical_rank: float = 0.0
    exact_match: bool = False
    description: str = ""
    outline: list[dict[str, Any]] = field(default_factory=list)
    index_digest: str = ""
    configuration_hash: str | None = None

    @property
    def signals(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.exact_match:
            values.append("exact_match")
        if self.fts_match or self.lexical_rank > 0:
            values.append("lexical_match")
        return tuple(values or ["catalog_reasoning"])


@dataclass(frozen=True)
class RankedDocument:
    document: Document
    rank: int
    signals: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class _CatalogBatch:
    candidates: tuple[CandidateDocument, ...]
    payload: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class RetrievalResult:
    evidence: list[Evidence]
    candidate_ids: list[uuid.UUID]
    selected_ids: list[uuid.UUID]
    sufficient: bool
    rounds: int
    ranked_documents: tuple[RankedDocument, ...] = ()
    unique_pages: int = 0
    evidence_tokens: int = 0
    overlap_pages_avoided: int = 0
    content_truncated: bool = False
    index_digests: dict[str, str] = field(default_factory=dict)
    strategy: str = "pageindex-tree-v2"
    strategy_version: str = "2"
    configuration_hash: str | None = None
    phase_timings_ms: dict[str, int] = field(default_factory=dict)

    @property
    def evidence_hash(self) -> str:
        payload = "\n".join(item.content for item in self.evidence)
        return hashlib.sha256(payload.encode()).hexdigest()


class Retriever(Protocol):
    async def retrieve(
        self,
        session: AsyncSession,
        question: str,
        constraints: MetadataConstraints,
        restrict_ids: list[uuid.UUID] | None = None,
        *,
        evidence_token_budget: int | None = None,
        index_version_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
    ) -> RetrievalResult: ...


class CorpusRouter:
    def __init__(self, settings: Settings, llm: StructuredLLM) -> None:
        self.settings = settings
        self.llm = llm
        self.encoding = tiktoken.get_encoding("cl100k_base")

    async def candidates(
        self,
        session: AsyncSession,
        question: str,
        constraints: MetadataConstraints,
        restrict_ids: list[uuid.UUID] | None = None,
        *,
        index_version_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
    ) -> list[CandidateDocument]:
        filters: list[Any] = []
        if index_version_ids is None:
            filters.extend(
                [
                    Document.status == DocumentStatus.ready,
                    Document.active_index_version_id.is_not(None),
                ]
            )
            index_join = and_(
                IndexVersion.id == Document.active_index_version_id,
                IndexVersion.document_id == Document.id,
            )
            title = Document.title
            abstract = Document.abstract
            description = Document.description
            topics = Document.topics
            submitted_date = Document.submitted_date
            search_vector = Document.search_vector
            metadata_payload = literal(None)
            metadata_page_count = literal(None)
        else:
            filters.append(
                tuple_(Document.id, IndexVersion.id).in_(tuple(index_version_ids.items()))
            )
            index_join = IndexVersion.document_id == Document.id
            metadata_payload = DocumentMetadataVersion.metadata_payload
            metadata_page_count = DocumentMetadataVersion.page_count
            title = metadata_payload["title"].astext
            abstract = metadata_payload["abstract"].astext
            description = metadata_payload["description"].astext
            topics = metadata_payload["topics"]
            submitted_date = sql_cast(
                func.nullif(metadata_payload["submitted_date"].astext, ""),
                Date,
            )
            search_vector = func.to_tsvector(
                "english",
                func.concat_ws(
                    " ",
                    title,
                    abstract,
                    description,
                    sql_cast(topics, Text),
                ),
            )
        if constraints.arxiv_ids:
            filters.append(Document.arxiv_id.in_(constraints.arxiv_ids))
        if constraints.authors:
            filters.append(
                Document.authors.overlap(constraints.authors)
                if index_version_ids is None
                else metadata_payload["authors"].op("?|")(array(constraints.authors))
            )
        if constraints.topics:
            filters.append(
                Document.topics.overlap(constraints.topics)
                if index_version_ids is None
                else topics.op("?|")(array(constraints.topics))
            )
        if constraints.date_from:
            filters.append(submitted_date >= constraints.date_from)
        if constraints.date_to:
            filters.append(submitted_date <= constraints.date_to)
        if restrict_ids is not None:
            filters.append(Document.id.in_(restrict_ids))

        search_text = candidate_search_text(question)
        strict_query = func.websearch_to_tsquery("english", search_text)
        tokens = fallback_search_tokens(search_text)
        signal_query = (
            func.websearch_to_tsquery("english", " or ".join(tokens)) if tokens else strict_query
        )
        rank = func.ts_rank_cd(search_vector, signal_query)
        strict_match = search_vector.op("@@")(strict_query)
        normalized_question = search_text.strip().casefold()
        exact = or_(
            func.lower(Document.arxiv_id) == normalized_question,
            func.lower(title) == normalized_question,
        )
        statement = select(
            Document,
            IndexVersion.id,
            IndexVersion.pageindex_description,
            IndexVersion.top_level_outline,
            IndexVersion.manifest_sha256,
            IndexVersion.artifact_sha256,
            IndexVersion.configuration_hash,
            metadata_payload,
            metadata_page_count,
            rank.label("lexical_rank"),
            strict_match.label("strict_match"),
            exact.label("exact_match"),
        ).join(IndexVersion, index_join)
        if index_version_ids is not None:
            statement = statement.join(
                DocumentMetadataVersion,
                and_(
                    DocumentMetadataVersion.id == IndexVersion.metadata_version_id,
                    DocumentMetadataVersion.document_id == Document.id,
                ),
            )
        statement = statement.where(and_(*filters)).order_by(
            case((exact, 0), else_=1),
            case((strict_match, 0), else_=1),
            rank.desc(),
            func.coalesce(Document.arxiv_id, literal("")),
            Document.id,
        )
        rows = (await session.execute(statement)).all()
        result: list[CandidateDocument] = []
        for (
            document,
            index_version_id,
            pageindex_description,
            top_level_outline,
            manifest_sha256,
            artifact_sha256,
            configuration_hash,
            raw_metadata,
            raw_metadata_page_count,
            raw_rank,
            raw_strict,
            raw_exact,
        ) in rows:
            selected_document = document
            if raw_metadata is not None:
                frozen_metadata = DocumentMetadata.model_validate(raw_metadata)
                selected_document = Document(
                    id=document.id,
                    sha256=document.sha256,
                    arxiv_id=document.arxiv_id,
                    original_filename=document.original_filename,
                    pdf_object_key=document.pdf_object_key,
                    title=frozen_metadata.title,
                    authors=frozen_metadata.authors,
                    abstract=frozen_metadata.abstract,
                    description=frozen_metadata.description,
                    topics=frozen_metadata.topics,
                    submitted_date=frozen_metadata.submitted_date,
                    status=DocumentStatus.ready,
                    page_count=int(raw_metadata_page_count),
                    active_index_version_id=index_version_id,
                )
            result.append(
                CandidateDocument(
                    document=selected_document,
                    fts_match=bool(raw_strict),
                    lexical_rank=float(raw_rank or 0),
                    exact_match=bool(raw_exact),
                    description=pageindex_description
                    or selected_document.description
                    or selected_document.abstract
                    or "",
                    outline=list(top_level_outline or []),
                    index_digest=manifest_sha256 or artifact_sha256,
                    configuration_hash=configuration_hash,
                )
            )
        return result

    def _payload(
        self,
        candidate: CandidateDocument,
        *,
        text_token_limit: int = MODEL_TEXT_TOKEN_LIMIT,
        title_token_limit: int | None = None,
        outline_limit: int = 64,
        cache: dict[str, list[int]] | None = None,
    ) -> dict[str, object]:
        token_cache = cache if cache is not None else {}

        def text(value: str, character_limit: int | None = None) -> str:
            bounded = value if character_limit is None else value[:character_limit]
            return _truncate_text(
                self.encoding,
                bounded,
                text_token_limit,
                token_cache,
            )

        outline: list[dict[str, object]] = []
        for raw in candidate.outline[:outline_limit]:
            value: dict[str, object] = {}
            for key in ("node_id", "page_start", "page_end"):
                if key in raw:
                    value[key] = raw[key]
            for key in ("title", "summary"):
                raw_text = raw.get(key)
                if isinstance(raw_text, str):
                    value[key] = text(raw_text)
            outline.append(value)
        return {
            "id": str(candidate.document.id),
            "arxiv_id": candidate.document.arxiv_id,
            "title": _truncate_text(
                self.encoding,
                (candidate.document.title or "")[:300],
                title_token_limit if title_token_limit is not None else text_token_limit,
                token_cache,
            ),
            "description": text(candidate.description, 4_000),
            "abstract": text(candidate.document.abstract or "", 1_200),
            "topics": [
                bounded for topic in candidate.document.topics[:8] if (bounded := text(topic, 80))
            ],
            "outline": outline,
            "signals": list(candidate.signals),
            "fts_match": candidate.fts_match,
            "lexical_rank": round(candidate.lexical_rank, 8),
        }

    def _map_payload(
        self,
        candidate: CandidateDocument,
        *,
        detail: int,
        cache: dict[str, list[int]],
    ) -> dict[str, object]:
        return self._payload(
            candidate,
            text_token_limit=detail,
            title_token_limit=max(int(bool(candidate.document.title)), detail),
            outline_limit=min(
                len(candidate.outline),
                CATALOG_MAP_OUTLINE_LIMIT,
                detail // CATALOG_MAP_OUTLINE_LIMIT,
            ),
            cache=cache,
        )

    def _batches(
        self,
        candidates: Sequence[CandidateDocument],
    ) -> list[_CatalogBatch]:
        limit = self.settings.catalog_batch_token_limit
        maximum_batch_size = self.settings.document_limit * 4
        batches: list[_CatalogBatch] = []
        offset = 0
        while offset < len(candidates):
            maximum_size = min(maximum_batch_size, len(candidates) - offset)
            batch_window = tuple(candidates[offset : offset + maximum_size])
            token_cache: dict[str, list[int]] = {}

            def payloads(
                size: int,
                detail: int,
                window: tuple[CandidateDocument, ...] = batch_window,
                cache: dict[str, list[int]] = token_cache,
            ) -> list[dict[str, object]]:
                return [
                    self._map_payload(candidate, detail=detail, cache=cache)
                    for candidate in window[:size]
                ]

            low = 1
            high = maximum_size
            fitting_size = 0
            while low <= high:
                middle = (low + high) // 2
                if _wrapped_token_count(self.encoding, payloads(middle, 0)) <= limit:
                    fitting_size = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if fitting_size == 0:
                raise ValueError("catalog batch token limit cannot fit a minimal candidate")

            low = 0
            high = CATALOG_MAP_DETAIL_LIMIT
            detail = 0
            while low <= high:
                middle = (low + high) // 2
                if _wrapped_token_count(self.encoding, payloads(fitting_size, middle)) <= limit:
                    detail = middle
                    low = middle + 1
                else:
                    high = middle - 1
            batch_candidates = tuple(batch_window[:fitting_size])
            batches.append(
                _CatalogBatch(
                    batch_candidates,
                    tuple(payloads(fitting_size, detail)),
                )
            )
            offset += fitting_size
        return batches

    def _comparison_payload(
        self,
        candidate: CandidateDocument,
        prior_reason: str,
        *,
        text_token_limit: int,
        cache: dict[str, list[int]],
    ) -> dict[str, object]:
        def text(value: str) -> str:
            return _truncate_text(
                self.encoding,
                value,
                text_token_limit,
                cache,
            )

        return {
            "id": str(candidate.document.id),
            "arxiv_id": candidate.document.arxiv_id,
            "title": text(candidate.document.title or candidate.document.original_filename),
            "synopsis": text(
                candidate.description
                or candidate.document.abstract
                or candidate.document.title
                or ""
            ),
            "prior_reason": text(prior_reason),
            "signals": list(candidate.signals),
            "fts_match": candidate.fts_match,
            "lexical_rank": round(candidate.lexical_rank, 8),
        }

    def _comparison_batches(
        self,
        pool: Sequence[tuple[CandidateDocument, str]],
    ) -> list[_CatalogBatch]:
        limit = self.settings.catalog_batch_token_limit
        maximum_batch_size = self.settings.document_limit * 4
        batches: list[_CatalogBatch] = []
        offset = 0
        while offset < len(pool):
            remaining = len(pool) - offset
            if remaining == 1:
                candidate, reason = pool[offset]
                payload = self._comparison_payload(
                    candidate,
                    reason,
                    text_token_limit=COMPARISON_TEXT_TOKEN_FLOOR,
                    cache={},
                )
                if _wrapped_token_count(self.encoding, [payload]) > limit:
                    raise ValueError(
                        "catalog batch token limit cannot fit a minimal reduction candidate"
                    )
                batches.append(_CatalogBatch((candidate,), (payload,)))
                break

            maximum_size = min(maximum_batch_size, remaining)

            def payloads(
                size: int,
                text_limit: int,
                start: int = offset,
            ) -> list[dict[str, object]]:
                cache: dict[str, list[int]] = {}
                return [
                    self._comparison_payload(
                        candidate,
                        reason,
                        text_token_limit=text_limit,
                        cache=cache,
                    )
                    for candidate, reason in pool[start : start + size]
                ]

            low = 2
            high = maximum_size
            fitting_size = 0
            while low <= high:
                middle = (low + high) // 2
                if (
                    _wrapped_token_count(
                        self.encoding,
                        payloads(middle, COMPARISON_TEXT_TOKEN_FLOOR),
                    )
                    <= limit
                ):
                    fitting_size = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if fitting_size < 2:
                raise ValueError(
                    "catalog batch token limit cannot fit two minimal reduction candidates"
                )

            low = COMPARISON_TEXT_TOKEN_FLOOR
            high = COMPARISON_TEXT_TOKEN_LIMIT
            text_limit = COMPARISON_TEXT_TOKEN_FLOOR
            while low <= high:
                middle = (low + high) // 2
                if (
                    _wrapped_token_count(
                        self.encoding,
                        payloads(fitting_size, middle),
                    )
                    <= limit
                ):
                    text_limit = middle
                    low = middle + 1
                else:
                    high = middle - 1
            batch_pool = pool[offset : offset + fitting_size]
            batches.append(
                _CatalogBatch(
                    tuple(candidate for candidate, _ in batch_pool),
                    tuple(payloads(fitting_size, text_limit)),
                )
            )
            offset += fitting_size
        return batches

    async def _select_batch(
        self,
        question: str,
        batch: _CatalogBatch,
        *,
        phase: str,
        selection_limit: int,
    ) -> list[tuple[CandidateDocument, str]]:
        candidates = batch.candidates
        if len(candidates) == 1 and phase == "reduce":
            return [
                (candidate, "Retained by complete-catalog reduction.") for candidate in candidates
            ]
        payload_json = _canonical_json(batch.payload)
        selection = await self.llm.structured(
            CandidateSelection,
            UNTRUSTED_DOCUMENT_SYSTEM
            + f"\nReason over every supplied catalog entry. Select at most {selection_limit} "
            "documents "
            "likely to contain answer evidence. Lexical and exact-match fields are ranking "
            "signals only; an entry without those signals remains eligible. "
            "Prefer documents marked fts_match when relevance is otherwise equal.",
            f"Question: {question}\nCatalog {phase} batch:\n"
            + wrap_untrusted_document(payload_json),
            thinking=True,
            stage=ModelCallStage.query_document_select,
        )
        allowed = {candidate.document.id: candidate for candidate in candidates}
        result: list[tuple[CandidateDocument, str]] = []
        for document_id in selection.document_ids:
            candidate = allowed.get(document_id)
            if candidate is None or candidate.document.id in {
                item[0].document.id for item in result
            }:
                continue
            result.append(
                (
                    candidate,
                    selection.reasons.get(
                        str(document_id),
                        "Selected by complete-catalog document reasoning.",
                    )[:2_000],
                )
            )
            if len(result) == selection_limit:
                break
        return result

    async def select_ranked(
        self,
        question: str,
        candidates: list[CandidateDocument],
    ) -> list[RankedDocument]:
        if not candidates:
            return []
        if len(candidates) <= self.settings.document_limit:
            return [
                RankedDocument(
                    candidate.document,
                    index,
                    candidate.signals,
                    "Eligible catalog contains at most eight documents.",
                )
                for index, candidate in enumerate(candidates, 1)
            ]
        mapped = [
            item
            for batch_result in await _gather_or_cancel(
                [
                    self._select_batch(
                        question,
                        batch,
                        phase="map",
                        selection_limit=self.settings.document_limit,
                    )
                    for batch in self._batches(candidates)
                ]
            )
            for item in batch_result
        ]
        deduplicated: dict[uuid.UUID, tuple[CandidateDocument, str]] = {}
        for candidate, reason in mapped:
            deduplicated.setdefault(candidate.document.id, (candidate, reason))
        pool = list(deduplicated.values())
        while len(pool) > self.settings.document_limit:
            reduced = [
                item
                for batch_result in await _gather_or_cancel(
                    [
                        self._select_batch(
                            question,
                            batch,
                            phase="reduce",
                            selection_limit=min(
                                self.settings.document_limit,
                                len(batch.candidates) - 1,
                            ),
                        )
                        for batch in self._comparison_batches(pool)
                    ]
                )
                for item in batch_result
            ]
            next_pool: dict[uuid.UUID, tuple[CandidateDocument, str]] = {}
            for candidate, reason in reduced:
                next_pool.setdefault(candidate.document.id, (candidate, reason))
            if len(next_pool) >= len(pool):
                raise AssertionError("catalog reduction did not shrink its candidate pool")
            pool = list(next_pool.values())
        return [
            RankedDocument(candidate.document, rank, candidate.signals, reason)
            for rank, (candidate, reason) in enumerate(pool, 1)
        ]

    async def select(
        self,
        question: str,
        candidates: list[CandidateDocument],
    ) -> list[Document]:
        return [item.document for item in await self.select_ranked(question, candidates)]


@dataclass(frozen=True)
class _NodeView:
    node_id: str
    title: str
    page_start: int
    page_end: int
    direct_summary: str
    subtree_summary: str
    parent_id: str | None
    depth: int
    section_path: tuple[str, ...]
    children: tuple[str, ...]


@dataclass(frozen=True)
class _ArtifactView:
    description: str
    nodes: dict[str, _NodeView]
    roots: tuple[str, ...]
    pages: dict[int, str]
    index_digest: str
    configuration_hash: str | None


@dataclass(frozen=True)
class _SelectedNode:
    document_id: uuid.UUID
    node: _NodeView
    reason: str


class PageIndexRetriever:
    def __init__(self, settings: Settings, store: ObjectStore, llm: StructuredLLM) -> None:
        self.settings = settings
        self.store = store
        self.llm = llm
        self.encoding = tiktoken.get_encoding("cl100k_base")

    @staticmethod
    def _node_from_v2(item: DerivedNode) -> _NodeView:
        node = item.node
        return _NodeView(
            node_id=node.node_id,
            title=node.title,
            page_start=node.subtree_page_range.page_start,
            page_end=node.subtree_page_range.page_end,
            direct_summary=node.direct_summary,
            subtree_summary=node.subtree_summary,
            parent_id=item.parent_id,
            depth=item.depth,
            section_path=item.section_path,
            children=tuple(child.node_id for child in node.children),
        )

    async def _artifact(
        self,
        session: AsyncSession,
        document: Document,
        index_version_id: uuid.UUID | None = None,
    ) -> _ArtifactView:
        selected_version_id = index_version_id or document.active_index_version_id
        if selected_version_id is None:
            raise ValueError(f"document {document.id} has no active index")
        version = await session.get(IndexVersion, selected_version_id)
        if version is None:
            raise ValueError(f"index {selected_version_id} is missing")
        if version.document_id != document.id:
            raise ValueError(
                f"index {selected_version_id} does not belong to document {document.id}"
            )
        if version.artifact_schema_version == 2:
            if version.manifest_object_key is None or version.manifest_sha256 is None:
                raise ValueError("PageIndex v2 version has no manifest")
            loaded = await load_manifest_v2(
                self.store,
                version.manifest_object_key,
                version.manifest_sha256,
            )
            derived = derive_nodes(loaded.manifest)
            nodes = {item.node.node_id: self._node_from_v2(item) for item in derived}
            roots = tuple(node.node_id for node in loaded.manifest.tree)
            return _ArtifactView(
                description=loaded.manifest.description,
                nodes=nodes,
                roots=roots,
                pages={page: metric.text for page, metric in loaded.pages.items()},
                index_digest=version.manifest_sha256,
                configuration_hash=version.configuration_hash,
            )

        raw = await self.store.get_bytes(version.artifact_object_key)
        if hashlib.sha256(raw).hexdigest() != version.artifact_sha256:
            raise ValueError(f"artifact checksum mismatch for document {document.id}")
        value: object = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid PageIndex v1 artifact")
        artifact = cast(dict[str, Any], value)
        if artifact.get("schema_version") != 1:
            raise ValueError("unsupported PageIndex artifact schema")
        raw_tree = artifact.get("tree")
        raw_pages = artifact.get("pages")
        if not isinstance(raw_tree, list) or not isinstance(raw_pages, list):
            raise ValueError("invalid PageIndex v1 artifact")
        nodes: dict[str, _NodeView] = {}
        for raw_node in cast(list[dict[str, Any]], raw_tree):
            node_id = str(raw_node["node_id"])
            start = int(raw_node["page_start"])
            end = int(raw_node["page_end"])
            title = str(raw_node["title"])
            summary = str(raw_node.get("summary") or title)
            nodes[node_id] = _NodeView(
                node_id=node_id,
                title=title,
                page_start=start,
                page_end=end,
                direct_summary=summary,
                subtree_summary=summary,
                parent_id=None,
                depth=0,
                section_path=(title,),
                children=(),
            )
        pages = {
            int(page["page"]): str(page["text"]) for page in cast(list[dict[str, Any]], raw_pages)
        }
        return _ArtifactView(
            description=str(artifact.get("description") or ""),
            nodes=nodes,
            roots=tuple(nodes),
            pages=pages,
            index_digest=version.artifact_sha256,
            configuration_hash=version.configuration_hash,
        )

    @staticmethod
    def _coerce_artifact(value: object) -> _ArtifactView:
        if isinstance(value, _ArtifactView):
            return value
        if not isinstance(value, dict):
            raise ValueError("invalid PageIndex artifact view")
        artifact = cast(dict[str, Any], value)
        raw_tree = artifact.get("tree")
        raw_pages = artifact.get("pages")
        if not isinstance(raw_tree, list) or not isinstance(raw_pages, list):
            raise ValueError("invalid PageIndex artifact view")
        nodes: dict[str, _NodeView] = {}
        for raw in cast(list[dict[str, Any]], raw_tree):
            node_id = str(raw["node_id"])
            title = str(raw.get("title") or node_id)
            summary = str(raw.get("summary") or title)
            nodes[node_id] = _NodeView(
                node_id=node_id,
                title=title,
                page_start=int(raw["page_start"]),
                page_end=int(raw["page_end"]),
                direct_summary=summary,
                subtree_summary=summary,
                parent_id=None,
                depth=0,
                section_path=(title,),
                children=(),
            )
        return _ArtifactView(
            description=str(artifact.get("description") or ""),
            nodes=nodes,
            roots=tuple(nodes),
            pages={
                int(page["page"]): str(page["text"])
                for page in cast(list[dict[str, Any]], raw_pages)
            },
            index_digest="legacy-v1",
            configuration_hash=None,
        )

    def _tree_node_payload(
        self,
        artifact: _ArtifactView,
        node_id: str,
        *,
        maximum_depth: int,
        used: set[str],
        text_token_limit: int,
        text_cache: dict[str, list[int]],
        visible_ids: set[str],
    ) -> dict[str, object]:
        node = artifact.nodes[node_id]
        visible_ids.add(node.node_id)
        children = (
            [
                self._tree_node_payload(
                    artifact,
                    child,
                    maximum_depth=maximum_depth,
                    used=used,
                    text_token_limit=text_token_limit,
                    text_cache=text_cache,
                    visible_ids=visible_ids,
                )
                for child in node.children
            ]
            if node.depth < maximum_depth
            else []
        )
        result: dict[str, object] = {
            "node_id": node.node_id,
            "pages": [node.page_start, node.page_end],
            "used": node.node_id in used,
            "children": children,
            "hidden_child_count": len(node.children) - len(children),
        }
        title = _truncate_text(
            self.encoding,
            node.title,
            text_token_limit,
            text_cache,
        )
        if title:
            result["title"] = title
        section_path = [
            value
            for item in node.section_path
            if (
                value := _truncate_text(
                    self.encoding,
                    item,
                    text_token_limit,
                    text_cache,
                )
            )
        ]
        if section_path:
            result["section_path"] = section_path
        collapsed = bool(node.children) and not children
        summary = _truncate_text(
            self.encoding,
            node.subtree_summary if collapsed else node.direct_summary,
            text_token_limit,
            text_cache,
        )
        if summary:
            result["subtree_summary" if collapsed else "direct_summary"] = summary
        return result

    def _tree_document_payload(
        self,
        document: Document,
        artifact: _ArtifactView,
        *,
        root_ids: Sequence[str],
        maximum_depth: int,
        used: set[str],
        text_token_limit: int,
        text_cache: dict[str, list[int]],
    ) -> tuple[dict[str, object], set[str]]:
        visible_ids: set[str] = set()
        tree = [
            self._tree_node_payload(
                artifact,
                node_id,
                maximum_depth=maximum_depth,
                used=used,
                text_token_limit=text_token_limit,
                text_cache=text_cache,
                visible_ids=visible_ids,
            )
            for node_id in root_ids
        ]
        result: dict[str, object] = {
            "document_id": str(document.id),
            "tree": tree,
            "omitted_root_count": len(artifact.roots) - len(root_ids),
        }
        title = _truncate_text(
            self.encoding,
            document.title or "",
            text_token_limit,
            text_cache,
        )
        if title:
            result["title"] = title
        description = _truncate_text(
            self.encoding,
            artifact.description,
            text_token_limit,
            text_cache,
        )
        if description:
            result["description"] = description
        return result, visible_ids

    def _bounded_tree_document(
        self,
        document: Document,
        artifact: _ArtifactView,
        *,
        round_number: int,
        used: set[str],
        token_limit: int,
    ) -> tuple[dict[str, object], set[str]]:
        text_cache: dict[str, list[int]] = {}
        roots = artifact.roots
        maximum_artifact_depth = max(
            (node.depth for node in artifact.nodes.values()),
            default=0,
        )
        requested_depth = min(round_number - 1, maximum_artifact_depth)

        def build(
            root_ids: Sequence[str],
            maximum_depth: int,
            text_limit: int,
        ) -> tuple[dict[str, object], set[str]]:
            return self._tree_document_payload(
                document,
                artifact,
                root_ids=root_ids,
                maximum_depth=maximum_depth,
                used=used,
                text_token_limit=text_limit,
                text_cache=text_cache,
            )

        selected_roots: Sequence[str] = roots
        selected_depth: int | None = None
        depth_candidates = [maximum_artifact_depth]
        depth_candidates.extend(
            depth for depth in range(requested_depth, -1, -1) if depth != maximum_artifact_depth
        )
        for maximum_depth in depth_candidates:
            structural, _ = build(roots, maximum_depth, 0)
            if _token_count(self.encoding, structural) <= token_limit:
                selected_depth = maximum_depth
                break

        if selected_depth is None:
            offset = (round_number - 1) % len(roots)
            rotated_roots = (*roots[offset:], *roots[:offset])
            selected: list[str] = []
            for root_id in rotated_roots:
                proposed = [*selected, root_id]
                structural, _ = build(proposed, 0, 0)
                if _token_count(self.encoding, structural) <= token_limit:
                    selected.append(root_id)
            selected_roots = selected
            selected_depth = 0

        low = 0
        high = MODEL_TEXT_TOKEN_LIMIT
        while low < high:
            middle = (low + high + 1) // 2
            candidate, _ = build(selected_roots, selected_depth, middle)
            if _token_count(self.encoding, candidate) <= token_limit:
                low = middle
            else:
                high = middle - 1
        return build(selected_roots, selected_depth, low)

    def _node_selection_payload(
        self,
        documents: list[Document],
        artifacts: dict[uuid.UUID, _ArtifactView],
        *,
        round_number: int,
        used_nodes: dict[uuid.UUID, set[str]],
    ) -> tuple[str | None, dict[uuid.UUID, set[str]], list[NodeChoice]]:
        forced: list[NodeChoice] = []
        eligible: list[tuple[Document, _ArtifactView]] = []
        visible_ids: dict[uuid.UUID, set[str]] = {}
        for document in documents:
            artifact = artifacts[document.id]
            if len(artifact.nodes) == 1:
                only = next(iter(artifact.nodes.values()))
                visible_ids[document.id] = {only.node_id}
                if only.node_id not in used_nodes[document.id]:
                    forced.append(
                        NodeChoice(
                            document_id=document.id,
                            node_id=only.node_id,
                            reason="The document contains one evidence node.",
                        )
                    )
            else:
                eligible.append((document, artifact))
                visible_ids[document.id] = set()

        if not eligible:
            return None, visible_ids, forced

        base_documents: list[dict[str, object]] = []
        for document, artifact in eligible:
            base_documents.append(
                {
                    "document_id": str(document.id),
                    "tree": [],
                    "omitted_root_count": len(artifact.roots),
                }
            )
        limit = self.settings.catalog_batch_token_limit
        base_payload: dict[str, object] = {"documents": base_documents}
        if _wrapped_token_count(self.encoding, base_payload) > limit:
            raise ValueError("node selection token limit cannot fit document envelopes")

        payload_documents: list[dict[str, object]] = list(base_documents)
        for index, (document, artifact) in enumerate(eligible):
            current_payload: dict[str, object] = {"documents": payload_documents}
            available = limit - _wrapped_token_count(self.encoding, current_payload)
            remaining_documents = len(eligible) - index
            share = available // remaining_documents
            base_tokens = _token_count(self.encoding, base_documents[index])
            candidate, candidate_visible = self._bounded_tree_document(
                document,
                artifact,
                round_number=round_number,
                used=used_nodes[document.id],
                token_limit=base_tokens + share,
            )
            proposed_documents: list[dict[str, object]] = list(payload_documents)
            proposed_documents[index] = candidate
            proposed: dict[str, object] = {"documents": proposed_documents}
            proposed_tokens = _wrapped_token_count(self.encoding, proposed)
            if proposed_tokens > limit:
                candidate, candidate_visible = self._bounded_tree_document(
                    document,
                    artifact,
                    round_number=round_number,
                    used=used_nodes[document.id],
                    token_limit=max(
                        base_tokens,
                        base_tokens + share - (proposed_tokens - limit) - 4,
                    ),
                )
                proposed_documents[index] = candidate
                proposed = {"documents": proposed_documents}
            if _wrapped_token_count(self.encoding, proposed) > limit:
                candidate = base_documents[index]
                candidate_visible = set[str]()
                proposed_documents[index] = candidate
            payload_documents = proposed_documents
            visible_ids[document.id] = candidate_visible

        payload = {"documents": payload_documents}
        payload_json = _canonical_json(payload)
        if _wrapped_token_count(self.encoding, payload_json) > limit:
            raise AssertionError("node selection payload exceeds its token limit")
        return payload_json, visible_ids, forced

    @staticmethod
    def _lineage(nodes: dict[str, _NodeView], node: _NodeView) -> set[str]:
        result = {node.node_id}
        parent = node.parent_id
        while parent is not None:
            result.add(parent)
            parent = nodes[parent].parent_id
        return result

    async def retrieve_detailed(
        self,
        session: AsyncSession,
        question: str,
        documents: list[Document],
        *,
        evidence_token_budget: int | None = None,
        index_version_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
    ) -> tuple[list[Evidence], bool, int, dict[str, object]]:
        async def load(document: Document) -> _ArtifactView | dict[str, object]:
            if index_version_ids is None:
                return await self._artifact(session, document)
            index_version_id = index_version_ids.get(document.id)
            if index_version_id is None:
                raise ValueError(f"document {document.id} has no pinned index")
            return await self._artifact(session, document, index_version_id)

        artifacts = {
            document.id: self._coerce_artifact(await load(document)) for document in documents
        }
        selected_nodes: list[_SelectedNode] = []
        used_nodes: defaultdict[uuid.UUID, set[str]] = defaultdict(set)
        prior_missing: str | None = None
        previous_pages: set[tuple[uuid.UUID, int]] = set()
        evidence: list[Evidence] = []
        metrics: dict[str, object] = {}
        for round_number in range(1, 4):
            payload_json, visible_ids, forced = self._node_selection_payload(
                documents,
                artifacts,
                round_number=round_number,
                used_nodes=used_nodes,
            )
            if payload_json is not None:
                selected = await self.llm.structured(
                    NodeSelection,
                    UNTRUSTED_DOCUMENT_SYSTEM
                    + "\nChoose marginally relevant PageIndex nodes. Do not choose used nodes "
                    "or redundant ancestor/descendant pairs. Use the missing-evidence note from "
                    "the prior round when present.",
                    f"Question: {question}\nPrior missing evidence: {prior_missing or 'none'}\n"
                    + wrap_untrusted_document(payload_json),
                    thinking=False,
                    stage=ModelCallStage.query_node_select,
                    max_output_tokens=self.settings.query_node_selection_max_output_tokens,
                )
                choices = (forced + selected.selections)[:32]
            else:
                choices = forced

            added = 0
            for choice in choices:
                artifact = artifacts.get(choice.document_id)
                if artifact is None:
                    continue
                node = artifact.nodes.get(choice.node_id)
                if (
                    node is None
                    or node.node_id not in visible_ids.get(choice.document_id, set())
                    or node.node_id in used_nodes[choice.document_id]
                ):
                    continue
                selected_for_document = [
                    item for item in selected_nodes if item.document_id == choice.document_id
                ]
                lineage = self._lineage(artifact.nodes, node)
                if any(
                    item.node.node_id in lineage
                    or node.node_id in self._lineage(artifact.nodes, item.node)
                    for item in selected_for_document
                ):
                    selected_pages = {
                        page
                        for item in selected_for_document
                        for page in range(item.node.page_start, item.node.page_end + 1)
                    }
                    if all(
                        page in selected_pages for page in range(node.page_start, node.page_end + 1)
                    ):
                        continue
                proposed_intervals = [
                    (item.node.page_start, item.node.page_end) for item in selected_for_document
                ]
                proposed_intervals.append((node.page_start, node.page_end))
                if (
                    len(self._merge_intervals(proposed_intervals))
                    > self.settings.page_ranges_per_document
                ):
                    continue
                selected_nodes.append(
                    _SelectedNode(choice.document_id, node, choice.reason[:2_000])
                )
                used_nodes[choice.document_id].add(node.node_id)
                added += 1

            evidence, assembly = self._assemble_evidence(
                documents,
                artifacts,
                selected_nodes,
                (
                    evidence_token_budget
                    if evidence_token_budget is not None
                    else self.settings.evidence_token_limit
                ),
            )
            current_pages = cast(set[tuple[uuid.UUID, int]], assembly["page_keys"])
            metrics = assembly
            if not current_pages - previous_pages or added == 0:
                return evidence, False, round_number, metrics
            previous_pages = current_pages
            sufficiency = await self.llm.structured(
                SufficiencyDecision,
                UNTRUSTED_DOCUMENT_SYSTEM
                + "\nDecide whether the evidence is sufficient for a grounded answer. "
                "When insufficient, describe the exact missing evidence.",
                f"Question: {question}\nEvidence:\n"
                + wrap_untrusted_document(self._evidence_text(evidence)),
                thinking=True,
                stage=ModelCallStage.query_sufficiency,
            )
            if sufficiency.sufficient:
                return evidence, True, round_number, metrics
            prior_missing = sufficiency.missing
        return evidence, False, 3, metrics

    async def retrieve(
        self,
        session: AsyncSession,
        question: str,
        documents: list[Document],
        *,
        evidence_token_budget: int | None = None,
    ) -> tuple[list[Evidence], bool, int]:
        evidence, sufficient, rounds, _ = await self.retrieve_detailed(
            session,
            question,
            documents,
            evidence_token_budget=evidence_token_budget,
        )
        return evidence, sufficient, rounds

    @staticmethod
    def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not intervals:
            return []
        ordered = sorted(intervals)
        merged = [ordered[0]]
        for start, end in ordered[1:]:
            prior_start, prior_end = merged[-1]
            if start <= prior_end + 1:
                merged[-1] = (prior_start, max(prior_end, end))
            else:
                merged.append((start, end))
        return merged

    def _assemble_evidence(
        self,
        documents: list[Document],
        artifacts: dict[uuid.UUID, _ArtifactView],
        selected: list[_SelectedNode],
        token_budget: int,
    ) -> tuple[list[Evidence], dict[str, object]]:
        document_map = {document.id: document for document in documents}
        by_document: defaultdict[uuid.UUID, list[_SelectedNode]] = defaultdict(list)
        for item in selected:
            by_document[item.document_id].append(item)
        result: list[Evidence] = []
        page_keys: set[tuple[uuid.UUID, int]] = set()
        tokens_used = 0
        overlap_avoided = 0
        truncated_any = False
        for document_id in [document.id for document in documents]:
            choices = by_document[document_id]
            if not choices:
                continue
            raw_intervals = [(item.node.page_start, item.node.page_end) for item in choices]
            raw_page_count = sum(end - start + 1 for start, end in raw_intervals)
            intervals = self._merge_intervals(raw_intervals)
            unique_page_count = sum(end - start + 1 for start, end in intervals)
            overlap_avoided += raw_page_count - unique_page_count
            artifact = artifacts[document_id]
            for start, end in intervals:
                if len(page_keys) >= self.settings.fetched_page_limit:
                    truncated_any = True
                    break
                if token_budget - tokens_used <= 0:
                    truncated_any = True
                    break
                content_parts: list[str] = []
                actual_start: int | None = None
                actual_end: int | None = None
                content_truncated = False
                for page in range(start, end + 1):
                    text = artifact.pages.get(page, "").strip()
                    if not text:
                        continue
                    if len(page_keys) >= self.settings.fetched_page_limit:
                        content_truncated = True
                        break
                    prefix = f"[Page {page}]\n"
                    page_content = prefix + text
                    proposed_content = "\n\n".join([*content_parts, page_content])
                    proposed_tokens = _token_count(self.encoding, proposed_content)
                    available_tokens = token_budget - tokens_used
                    if proposed_tokens > available_tokens:
                        encoded_text = self.encoding.encode(text, disallowed_special=())
                        low = 1
                        high = len(encoded_text)
                        best_content = ""
                        best_page_content = ""
                        while low <= high:
                            middle = (low + high) // 2
                            partial_text = self.encoding.decode(encoded_text[:middle])
                            candidate_page_content = prefix + partial_text
                            candidate_content = "\n\n".join(
                                [*content_parts, candidate_page_content]
                            )
                            if (
                                partial_text.strip()
                                and _token_count(self.encoding, candidate_content)
                                <= available_tokens
                            ):
                                best_content = candidate_content
                                best_page_content = candidate_page_content
                                low = middle + 1
                            else:
                                high = middle - 1
                        if best_content:
                            content_parts.append(best_page_content)
                            page_keys.add((document_id, page))
                            if actual_start is None:
                                actual_start = page
                            actual_end = page
                        content_truncated = True
                        break
                    content_parts.append(page_content)
                    page_keys.add((document_id, page))
                    if actual_start is None:
                        actual_start = page
                    actual_end = page
                if not content_parts or actual_start is None or actual_end is None:
                    truncated_any = True
                    continue
                content = "\n\n".join(content_parts)
                content_tokens = _token_count(self.encoding, content)
                if tokens_used + content_tokens > token_budget:
                    raise AssertionError("assembled evidence exceeds its token budget")
                contributors = [
                    item
                    for item in choices
                    if item.node.page_start <= actual_end and item.node.page_end >= actual_start
                ]
                document = document_map[document_id]
                result.append(
                    Evidence(
                        source_type="document",
                        source_id=str(document.id),
                        title=document.title or document.original_filename,
                        node_id=contributors[0].node.node_id,
                        node_ids=[item.node.node_id for item in contributors],
                        section_paths=[list(item.node.section_path) for item in contributors],
                        page_start=actual_start,
                        page_end=actual_end,
                        content=content,
                        content_truncated=content_truncated,
                        retrieval_reason="; ".join(
                            dict.fromkeys(item.reason for item in contributors)
                        )[:4_000],
                    )
                )
                tokens_used += content_tokens
                truncated_any = truncated_any or content_truncated
        actual_tokens = sum(_token_count(self.encoding, item.content) for item in result)
        if actual_tokens != tokens_used:
            raise AssertionError("evidence token accounting drifted from emitted content")
        return result, {
            "page_keys": page_keys,
            "unique_pages": len(page_keys),
            "evidence_tokens": tokens_used,
            "overlap_pages_avoided": overlap_avoided,
            "content_truncated": truncated_any,
        }

    @staticmethod
    def _evidence_text(evidence: list[Evidence]) -> str:
        return "\n\n".join(
            f"Evidence {index}: {item.title}, pages "
            f"{item.page_start}-{item.page_end}\n{item.content}"
            for index, item in enumerate(evidence, 1)
        )


class VectorlessRetriever:
    def __init__(
        self,
        settings: Settings,
        router: CorpusRouter,
        pageindex: PageIndexRetriever,
    ) -> None:
        self.settings = settings
        self.router = router
        self.pageindex = pageindex

    async def retrieve(
        self,
        session: AsyncSession,
        question: str,
        constraints: MetadataConstraints,
        restrict_ids: list[uuid.UUID] | None = None,
        *,
        evidence_token_budget: int | None = None,
        index_version_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
    ) -> RetrievalResult:
        started = time.monotonic()
        candidates = await self.router.candidates(
            session,
            question,
            constraints,
            restrict_ids,
            index_version_ids=index_version_ids,
        )
        selected = await self.router.select_ranked(question, candidates)
        selection_ms = round((time.monotonic() - started) * 1000)
        if not selected:
            return RetrievalResult(
                [],
                [item.document.id for item in candidates],
                [],
                False,
                0,
                phase_timings_ms={"catalog_selection": selection_ms},
            )
        retrieval_started = time.monotonic()
        documents = [item.document for item in selected]
        evidence, sufficient, rounds, metrics = await self.pageindex.retrieve_detailed(
            session,
            question,
            documents,
            evidence_token_budget=evidence_token_budget,
            index_version_ids=index_version_ids,
        )
        retrieval_ms = round((time.monotonic() - retrieval_started) * 1000)
        configuration_hashes = sorted(
            {
                candidate.configuration_hash
                for candidate in candidates
                if candidate.document.id in {item.document.id for item in selected}
                and candidate.configuration_hash is not None
            }
        )
        configuration_hash = hashlib.sha256("\n".join(configuration_hashes).encode()).hexdigest()
        unique_pages = metrics.get("unique_pages")
        evidence_tokens = metrics.get("evidence_tokens")
        overlap_pages_avoided = metrics.get("overlap_pages_avoided")
        return RetrievalResult(
            evidence=evidence,
            candidate_ids=[item.document.id for item in candidates],
            selected_ids=[item.document.id for item in selected],
            sufficient=sufficient,
            rounds=rounds,
            ranked_documents=tuple(selected),
            unique_pages=unique_pages if isinstance(unique_pages, int) else 0,
            evidence_tokens=evidence_tokens if isinstance(evidence_tokens, int) else 0,
            overlap_pages_avoided=(
                overlap_pages_avoided if isinstance(overlap_pages_avoided, int) else 0
            ),
            content_truncated=bool(metrics.get("content_truncated", False)),
            configuration_hash=configuration_hash,
            index_digests={
                str(candidate.document.id): candidate.index_digest
                for candidate in candidates
                if candidate.document.id in {item.document.id for item in selected}
            },
            phase_timings_ms={
                "catalog_selection": selection_ms,
                "tree_retrieval": retrieval_ms,
            },
        )
