from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import tiktoken
from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings
from vectorless_rag.llm import UNTRUSTED_DOCUMENT_SYSTEM, StructuredLLM, wrap_untrusted_document
from vectorless_rag.models import Document, DocumentStatus, IndexVersion, ModelCallStage
from vectorless_rag.pageindex_artifact import (
    DerivedNode,
    derive_nodes,
    load_manifest_v2,
)
from vectorless_rag.schemas import (
    CandidateSelection,
    Evidence,
    MetadataConstraints,
    NodeChoice,
    NodeSelection,
    SufficiencyDecision,
)
from vectorless_rag.storage import ObjectStore

QUOTED_PHRASE = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
FALLBACK_SEARCH_EXCLUDED_TOKENS = frozenset({"or", "and", "not"})


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
    ) -> list[CandidateDocument]:
        filters: list[Any] = [
            Document.status == DocumentStatus.ready,
            Document.active_index_version_id.is_not(None),
        ]
        if constraints.arxiv_ids:
            filters.append(Document.arxiv_id.in_(constraints.arxiv_ids))
        if constraints.authors:
            filters.append(Document.authors.overlap(constraints.authors))
        if constraints.topics:
            filters.append(Document.topics.overlap(constraints.topics))
        if constraints.date_from:
            filters.append(Document.submitted_date >= constraints.date_from)
        if constraints.date_to:
            filters.append(Document.submitted_date <= constraints.date_to)
        if restrict_ids is not None:
            filters.append(Document.id.in_(restrict_ids))

        search_text = candidate_search_text(question)
        strict_query = func.websearch_to_tsquery("english", search_text)
        tokens = fallback_search_tokens(search_text)
        signal_query = (
            func.websearch_to_tsquery("english", " or ".join(tokens)) if tokens else strict_query
        )
        rank = func.ts_rank_cd(Document.search_vector, signal_query)
        strict_match = Document.search_vector.op("@@")(strict_query)
        normalized_question = search_text.strip().casefold()
        exact = or_(
            func.lower(Document.arxiv_id) == normalized_question,
            func.lower(Document.title) == normalized_question,
        )
        statement = (
            select(
                Document,
                rank.label("lexical_rank"),
                strict_match.label("strict_match"),
                exact.label("exact_match"),
            )
            .where(and_(*filters))
            .order_by(
                case((exact, 0), else_=1),
                case((strict_match, 0), else_=1),
                rank.desc(),
                func.coalesce(Document.arxiv_id, literal("")),
                Document.id,
            )
        )
        rows = (await session.execute(statement)).all()
        result: list[CandidateDocument] = []
        for document, raw_rank, raw_strict, raw_exact in rows:
            version = await session.get(IndexVersion, document.active_index_version_id)
            if version is None:
                continue
            result.append(
                CandidateDocument(
                    document=document,
                    fts_match=bool(raw_strict),
                    lexical_rank=float(raw_rank or 0),
                    exact_match=bool(raw_exact),
                    description=version.pageindex_description
                    or document.description
                    or document.abstract
                    or "",
                    outline=list(version.top_level_outline or []),
                    index_digest=version.manifest_sha256 or version.artifact_sha256,
                    configuration_hash=version.configuration_hash,
                )
            )
        return result

    def _payload(self, candidate: CandidateDocument) -> dict[str, object]:
        return {
            "id": str(candidate.document.id),
            "arxiv_id": candidate.document.arxiv_id,
            "title": (candidate.document.title or "")[:300],
            "description": candidate.description[:4_000],
            "abstract": (candidate.document.abstract or "")[:1_200],
            "topics": [topic[:80] for topic in candidate.document.topics[:8]],
            "outline": candidate.outline[:64],
            "signals": list(candidate.signals),
            "fts_match": candidate.fts_match,
            "lexical_rank": round(candidate.lexical_rank, 8),
        }

    def _batches(
        self,
        candidates: Sequence[CandidateDocument],
    ) -> list[list[CandidateDocument]]:
        limit = self.settings.catalog_batch_token_limit
        batches: list[list[CandidateDocument]] = []
        current: list[CandidateDocument] = []
        tokens = 0
        for candidate in candidates:
            payload_tokens = len(
                self.encoding.encode(
                    json.dumps(self._payload(candidate), sort_keys=True, separators=(",", ":"))
                )
            )
            if current and tokens + payload_tokens > limit:
                batches.append(current)
                current = []
                tokens = 0
            current.append(candidate)
            tokens += payload_tokens
        if current:
            batches.append(current)
        return batches

    async def _select_batch(
        self,
        question: str,
        candidates: Sequence[CandidateDocument],
        *,
        phase: str,
    ) -> list[tuple[CandidateDocument, str]]:
        if len(candidates) <= self.settings.document_limit and phase == "reduce":
            return [
                (candidate, "Retained by complete-catalog reduction.") for candidate in candidates
            ]
        payload = [self._payload(candidate) for candidate in candidates]
        selection = await self.llm.structured(
            CandidateSelection,
            UNTRUSTED_DOCUMENT_SYSTEM
            + "\nReason over every supplied catalog entry. Select at most eight documents "
            "likely to contain answer evidence. Lexical and exact-match fields are ranking "
            "signals only; an entry without those signals remains eligible. "
            "Prefer documents marked fts_match when relevance is otherwise equal.",
            f"Question: {question}\nCatalog {phase} batch:\n"
            + wrap_untrusted_document(json.dumps(payload, sort_keys=True)),
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
            if len(result) == self.settings.document_limit:
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
        mapped: list[tuple[CandidateDocument, str]] = []
        for batch in self._batches(candidates):
            mapped.extend(await self._select_batch(question, batch, phase="map"))
        deduplicated: dict[uuid.UUID, tuple[CandidateDocument, str]] = {}
        for candidate, reason in mapped:
            deduplicated.setdefault(candidate.document.id, (candidate, reason))
        pool = list(deduplicated.values())
        while len(pool) > self.settings.document_limit:
            reduced: list[tuple[CandidateDocument, str]] = []
            for offset in range(0, len(pool), self.settings.document_limit * 4):
                batch = [
                    item[0] for item in pool[offset : offset + self.settings.document_limit * 4]
                ]
                reduced.extend(await self._select_batch(question, batch, phase="reduce"))
            next_pool: dict[uuid.UUID, tuple[CandidateDocument, str]] = {}
            for candidate, reason in reduced:
                next_pool.setdefault(candidate.document.id, (candidate, reason))
            if len(next_pool) >= len(pool):
                pool = pool[: self.settings.document_limit]
                break
            pool = list(next_pool.values())
        return [
            RankedDocument(candidate.document, rank, candidate.signals, reason)
            for rank, (candidate, reason) in enumerate(pool[: self.settings.document_limit], 1)
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

    async def _artifact(self, session: AsyncSession, document: Document) -> _ArtifactView:
        if document.active_index_version_id is None:
            raise ValueError(f"document {document.id} has no active index")
        version = await session.get(IndexVersion, document.active_index_version_id)
        if version is None:
            raise ValueError(f"active index {document.active_index_version_id} is missing")
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

    def _visible_tree(
        self,
        artifact: _ArtifactView,
        *,
        round_number: int,
        used: set[str],
    ) -> list[dict[str, object]]:
        def serialize(node_id: str, maximum_depth: int) -> dict[str, object]:
            node = artifact.nodes[node_id]
            children = (
                [serialize(child, maximum_depth) for child in node.children]
                if node.depth < maximum_depth
                else []
            )
            return {
                "node_id": node.node_id,
                "title": node.title,
                "pages": [node.page_start, node.page_end],
                "direct_summary": node.direct_summary,
                "subtree_summary": node.subtree_summary,
                "section_path": list(node.section_path),
                "used": node.node_id in used,
                "children": children,
                "hidden_child_count": len(node.children) - len(children),
            }

        full_depth = max((node.depth for node in artifact.nodes.values()), default=0)
        full = [serialize(node_id, full_depth) for node_id in artifact.roots]
        if (
            len(self.encoding.encode(json.dumps(full, sort_keys=True, separators=(",", ":"))))
            <= self.settings.catalog_batch_token_limit
        ):
            return full
        maximum_depth = round_number - 1
        return [serialize(node_id, maximum_depth) for node_id in artifact.roots]

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
    ) -> tuple[list[Evidence], bool, int, dict[str, object]]:
        artifacts = {
            document.id: self._coerce_artifact(await self._artifact(session, document))
            for document in documents
        }
        selected_nodes: list[_SelectedNode] = []
        used_nodes: defaultdict[uuid.UUID, set[str]] = defaultdict(set)
        prior_missing: str | None = None
        previous_pages: set[tuple[uuid.UUID, int]] = set()
        evidence: list[Evidence] = []
        metrics: dict[str, object] = {}
        for round_number in range(1, 4):
            forced: list[NodeChoice] = []
            tree_payload: list[dict[str, object]] = []
            for document in documents:
                artifact = artifacts[document.id]
                if len(artifact.nodes) == 1:
                    only = next(iter(artifact.nodes.values()))
                    if only.node_id not in used_nodes[document.id]:
                        forced.append(
                            NodeChoice(
                                document_id=document.id,
                                node_id=only.node_id,
                                reason="The document contains one evidence node.",
                            )
                        )
                else:
                    tree_payload.append(
                        {
                            "document_id": str(document.id),
                            "title": document.title,
                            "description": artifact.description,
                            "tree": self._visible_tree(
                                artifact,
                                round_number=round_number,
                                used=used_nodes[document.id],
                            ),
                        }
                    )
            if tree_payload:
                selected = await self.llm.structured(
                    NodeSelection,
                    UNTRUSTED_DOCUMENT_SYSTEM
                    + "\nChoose marginally relevant PageIndex nodes. Do not choose used nodes "
                    "or redundant ancestor/descendant pairs. Use the missing-evidence note from "
                    "the prior round when present.",
                    f"Question: {question}\nPrior missing evidence: {prior_missing or 'none'}\n"
                    + wrap_untrusted_document(
                        json.dumps({"documents": tree_payload}, sort_keys=True)
                    ),
                    thinking=True,
                    stage=ModelCallStage.query_node_select,
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
                visible_tree = self._visible_tree(
                    artifact,
                    round_number=round_number,
                    used=used_nodes[choice.document_id],
                )
                visible_ids: set[str] = set()
                pending = list(visible_tree)
                while pending:
                    value = pending.pop()
                    visible_ids.add(str(value["node_id"]))
                    pending.extend(cast(list[dict[str, object]], value["children"]))
                if (
                    node is None
                    or node.node_id not in visible_ids
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
                selected_nodes.append(
                    _SelectedNode(choice.document_id, node, choice.reason[:2_000])
                )
                used_nodes[choice.document_id].add(node.node_id)
                added += 1

            evidence, assembly = self._assemble_evidence(
                documents,
                artifacts,
                selected_nodes,
                evidence_token_budget or self.settings.evidence_token_limit,
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

    def _fetch_evidence(
        self,
        question: str,
        documents: list[Document],
        artifacts: dict[uuid.UUID, dict[str, Any]],
        selection: NodeSelection,
        existing: list[Evidence],
        used_nodes: set[tuple[uuid.UUID, str]],
    ) -> list[Evidence]:
        del question
        document_map = {document.id: document for document in documents}
        views = {
            document_id: self._coerce_artifact(artifact)
            for document_id, artifact in artifacts.items()
        }
        selected: list[_SelectedNode] = []
        ranges_per_document: defaultdict[uuid.UUID, int] = defaultdict(int)
        for item in existing:
            ranges_per_document[uuid.UUID(item.source_id)] += 1
        for choice in selection.selections:
            key = (choice.document_id, choice.node_id)
            view = views.get(choice.document_id)
            if (
                key in used_nodes
                or view is None
                or choice.document_id not in document_map
                or ranges_per_document[choice.document_id] >= self.settings.page_ranges_per_document
            ):
                continue
            node = view.nodes.get(choice.node_id)
            if node is None:
                continue
            selected.append(_SelectedNode(choice.document_id, node, choice.reason))
            used_nodes.add(key)
            ranges_per_document[choice.document_id] += 1
        assembled, _ = self._assemble_evidence(
            documents,
            views,
            selected,
            self.settings.evidence_token_limit,
        )
        return [*existing, *assembled]

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
                remaining_tokens = token_budget - tokens_used
                if remaining_tokens <= 0:
                    truncated_any = True
                    break
                content_parts: list[str] = []
                actual_start: int | None = None
                actual_end: int | None = None
                content_truncated = False
                for page in range(start, end + 1):
                    text = artifact.pages.get(page, "")
                    if not text.strip():
                        continue
                    if len(page_keys) >= self.settings.fetched_page_limit:
                        content_truncated = True
                        break
                    prefix = f"[Page {page}]\n"
                    encoded = self.encoding.encode(prefix + text)
                    if len(encoded) > remaining_tokens:
                        partial = self.encoding.decode(encoded[:remaining_tokens])
                        if partial.strip():
                            content_parts.append(partial)
                            page_keys.add((document_id, page))
                            actual_start = actual_start or page
                            actual_end = page
                            tokens_used += remaining_tokens
                        content_truncated = True
                        break
                    content_parts.append(prefix + text)
                    page_keys.add((document_id, page))
                    actual_start = actual_start or page
                    actual_end = page
                    tokens_used += len(encoded)
                    remaining_tokens -= len(encoded)
                if not content_parts or actual_start is None or actual_end is None:
                    continue
                contributors = [
                    item
                    for item in choices
                    if item.node.page_start <= actual_end and item.node.page_end >= start
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
                        content="\n\n".join(content_parts),
                        content_truncated=content_truncated,
                        retrieval_reason="; ".join(
                            dict.fromkeys(item.reason for item in contributors)
                        )[:4_000],
                    )
                )
                truncated_any = truncated_any or content_truncated
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
    ) -> RetrievalResult:
        started = time.monotonic()
        candidates = await self.router.candidates(session, question, constraints, restrict_ids)
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
