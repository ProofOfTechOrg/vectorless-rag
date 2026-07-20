from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, cast

import tiktoken
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings
from vectorless_rag.llm import UNTRUSTED_DOCUMENT_SYSTEM, StructuredLLM, wrap_untrusted_document
from vectorless_rag.models import Document, DocumentStatus, IndexVersion
from vectorless_rag.schemas import (
    CandidateSelection,
    Evidence,
    MetadataConstraints,
    NodeSelection,
    SufficiencyDecision,
)
from vectorless_rag.storage import ObjectStore


@dataclass(frozen=True)
class RetrievalResult:
    evidence: list[Evidence]
    candidate_ids: list[uuid.UUID]
    selected_ids: list[uuid.UUID]
    sufficient: bool
    rounds: int

    @property
    def evidence_hash(self) -> str:
        payload = "\n".join(item.content for item in self.evidence)
        return hashlib.sha256(payload.encode()).hexdigest()


class CorpusRouter:
    def __init__(self, settings: Settings, llm: StructuredLLM) -> None:
        self.settings = settings
        self.llm = llm

    async def candidates(
        self,
        session: AsyncSession,
        question: str,
        constraints: MetadataConstraints,
        restrict_ids: list[uuid.UUID] | None = None,
    ) -> list[Document]:
        filters: list[Any] = [Document.status == DocumentStatus.ready]
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
        query = func.websearch_to_tsquery("english", question)
        rank = func.ts_rank_cd(Document.search_vector, query)
        text_filter = Document.search_vector.op("@@")(query)
        statement = (
            select(Document)
            .where(and_(*filters), or_(text_filter, Document.arxiv_id.in_([question])))
            .order_by(rank.desc(), Document.created_at.desc())
            .limit(self.settings.candidate_limit)
        )
        return list((await session.scalars(statement)).all())

    async def select(self, question: str, candidates: list[Document]) -> list[Document]:
        if len(candidates) <= self.settings.document_limit:
            return candidates
        candidate_payload = [
            {
                "id": str(document.id),
                "arxiv_id": document.arxiv_id,
                "title": document.title,
                "abstract": (document.abstract or "")[:2_000],
                "topics": document.topics,
            }
            for document in candidates
        ]
        selection = await self.llm.structured(
            CandidateSelection,
            UNTRUSTED_DOCUMENT_SYSTEM
            + "\nSelect only documents likely to contain evidence. Return at most eight IDs.",
            f"Question: {question}\nCandidates:\n"
            + wrap_untrusted_document(json.dumps(candidate_payload)),
            thinking=True,
        )
        allowed = {document.id: document for document in candidates}
        selected: list[Document] = []
        for document_id in selection.document_ids:
            if document_id in allowed and document_id not in {item.id for item in selected}:
                selected.append(allowed[document_id])
            if len(selected) == self.settings.document_limit:
                break
        return selected


class PageIndexRetriever:
    def __init__(self, settings: Settings, store: ObjectStore, llm: StructuredLLM) -> None:
        self.settings = settings
        self.store = store
        self.llm = llm
        self.encoding = tiktoken.get_encoding("cl100k_base")

    async def _artifact(self, session: AsyncSession, document: Document) -> dict[str, Any]:
        if document.active_index_version_id is None:
            raise ValueError(f"document {document.id} has no active index")
        version = await session.get(IndexVersion, document.active_index_version_id)
        if version is None:
            raise ValueError(f"active index {document.active_index_version_id} is missing")
        raw = await self.store.get_bytes(version.artifact_object_key)
        if hashlib.sha256(raw).hexdigest() != version.artifact_sha256:
            raise ValueError(f"artifact checksum mismatch for document {document.id}")
        artifact = cast(dict[str, Any], json.loads(raw))
        if artifact.get("schema_version") != 1:
            raise ValueError("unsupported PageIndex artifact schema")
        return artifact

    async def retrieve(
        self, session: AsyncSession, question: str, documents: list[Document]
    ) -> tuple[list[Evidence], bool, int]:
        artifacts = {document.id: await self._artifact(session, document) for document in documents}
        evidence: list[Evidence] = []
        used_nodes: set[tuple[uuid.UUID, str]] = set()
        for round_number in range(1, 3):
            tree_payload = [
                {
                    "document_id": str(document.id),
                    "title": document.title,
                    "tree": artifacts[document.id]["tree"],
                }
                for document in documents
            ]
            prior = [
                {
                    "source_id": item.source_id,
                    "node_id": item.node_id,
                    "pages": [item.page_start, item.page_end],
                    "preview": item.content[:500],
                }
                for item in evidence
            ]
            selection_prompt = (
                f"Question: {question}\nTrees and prior retrieval:\n"
                + wrap_untrusted_document(json.dumps({"trees": tree_payload, "prior": prior}))
            )
            selection = await self.llm.structured(
                NodeSelection,
                UNTRUSTED_DOCUMENT_SYSTEM
                + "\nChoose tree nodes whose page ranges can directly answer the question.",
                selection_prompt,
                thinking=True,
            )
            evidence = self._fetch_evidence(
                question, documents, artifacts, selection, evidence, used_nodes
            )
            sufficiency_prompt = (
                f"Question: {question}\nEvidence:\n"
                f"{wrap_untrusted_document(self._evidence_text(evidence))}"
            )
            sufficiency = await self.llm.structured(
                SufficiencyDecision,
                UNTRUSTED_DOCUMENT_SYSTEM
                + "\nDecide whether the evidence is sufficient for a grounded answer.",
                sufficiency_prompt,
                thinking=True,
            )
            if sufficiency.sufficient:
                return evidence, True, round_number
        return evidence, False, 2

    def _fetch_evidence(
        self,
        question: str,
        documents: list[Document],
        artifacts: dict[uuid.UUID, dict[str, Any]],
        selection: NodeSelection,
        existing: list[Evidence],
        used_nodes: set[tuple[uuid.UUID, str]],
    ) -> list[Evidence]:
        result = list(existing)
        document_map = {document.id: document for document in documents}
        ranges_per_document: defaultdict[uuid.UUID, int] = defaultdict(int)
        for item in existing:
            ranges_per_document[uuid.UUID(item.source_id)] += 1
        pages_used = sum((item.page_end or 0) - (item.page_start or 1) + 1 for item in existing)
        tokens_used = sum(len(self.encoding.encode(item.content)) for item in existing)
        for choice in selection.selections:
            key = (choice.document_id, choice.node_id)
            if key in used_nodes or choice.document_id not in document_map:
                continue
            if ranges_per_document[choice.document_id] >= self.settings.page_ranges_per_document:
                continue
            artifact = artifacts[choice.document_id]
            node = next(
                (node for node in artifact["tree"] if node["node_id"] == choice.node_id), None
            )
            if node is None:
                continue
            start = max(1, int(node["page_start"]))
            end = max(start, int(node["page_end"]))
            remaining_pages = self.settings.fetched_page_limit - pages_used
            if remaining_pages <= 0:
                break
            end = min(end, start + remaining_pages - 1)
            page_map = {int(page["page"]): str(page["text"]) for page in artifact["pages"]}
            content = "\n\n".join(
                f"[Page {page}]\n{page_map.get(page, '')}" for page in range(start, end + 1)
            ).strip()
            remaining_tokens = self.settings.evidence_token_limit - tokens_used
            encoded = self.encoding.encode(content)
            if remaining_tokens <= 0:
                break
            if len(encoded) > remaining_tokens:
                content = self.encoding.decode(encoded[:remaining_tokens])
                encoded = encoded[:remaining_tokens]
            document = document_map[choice.document_id]
            result.append(
                Evidence(
                    source_type="document",
                    source_id=str(document.id),
                    title=document.title or document.original_filename,
                    node_id=choice.node_id,
                    page_start=start,
                    page_end=end,
                    content=content,
                    retrieval_reason=choice.reason,
                )
            )
            used_nodes.add(key)
            ranges_per_document[choice.document_id] += 1
            pages_used += end - start + 1
            tokens_used += len(encoded)
        return result

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
    ) -> RetrievalResult:
        candidates = await self.router.candidates(session, question, constraints, restrict_ids)
        selected = await self.router.select(question, candidates)
        if not selected:
            return RetrievalResult([], [item.id for item in candidates], [], False, 0)
        evidence, sufficient, rounds = await self.pageindex.retrieve(session, question, selected)
        return RetrievalResult(
            evidence,
            [item.id for item in candidates],
            [item.id for item in selected],
            sufficient,
            rounds,
        )
