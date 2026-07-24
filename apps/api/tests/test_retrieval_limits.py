import json
import uuid
from typing import TypeVar

from pydantic import BaseModel
from pytest import MonkeyPatch

from vectorless_rag.config import Settings
from vectorless_rag.models import Document, ModelCallStage
from vectorless_rag.retrieval import (
    CandidateDocument,
    CorpusRouter,
    PageIndexRetriever,
    candidate_search_text,
    fallback_search_tokens,
)
from vectorless_rag.schemas import (
    CandidateSelection,
    NodeChoice,
    NodeSelection,
    SufficiencyDecision,
)
from vectorless_rag.storage import MemoryObjectStore

StructuredT = TypeVar("StructuredT", bound=BaseModel)


class UnusedLLM:
    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
    ) -> StructuredT:
        del schema, system, user, thinking, stage
        raise AssertionError("the evidence cap test does not invoke the LLM")


def test_evidence_enforces_range_page_and_token_caps() -> None:
    settings = Settings(
        page_ranges_per_document=1,
        fetched_page_limit=2,
        evidence_token_limit=30,
    )
    retriever = PageIndexRetriever(settings, MemoryObjectStore(), UnusedLLM())
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        sha256="a" * 64,
        original_filename="paper.pdf",
        pdf_object_key="pdf/key",
        title="Paper",
    )
    artifact = {
        "tree": [
            {"node_id": "n1", "page_start": 1, "page_end": 10},
            {"node_id": "n2", "page_start": 3, "page_end": 3},
        ],
        "pages": [{"page": index, "text": "evidence " * 100} for index in range(1, 11)],
    }
    selection = NodeSelection(
        selections=[
            NodeChoice(document_id=document_id, node_id="n1", reason="relevant"),
            NodeChoice(document_id=document_id, node_id="n2", reason="also relevant"),
        ]
    )
    evidence = retriever._fetch_evidence(  # pyright: ignore[reportPrivateUsage]
        "question", [document], {document_id: artifact}, selection, [], set()
    )
    assert len(evidence) == 1
    assert evidence[0].page_end == 2
    assert len(retriever.encoding.encode(evidence[0].content)) <= 30


def test_candidate_search_prefers_quoted_phrase() -> None:
    assert (
        candidate_search_text('Which document contains "cobalt orbit unique"? Cite its page.')
        == "cobalt orbit unique"
    )
    assert candidate_search_text("Compare the indexed papers") == "Compare the indexed papers"


def test_fallback_search_tokens_are_safe_deduplicated_and_bounded() -> None:
    question = "OR Alpha, alpha -Transformer AND beta NOT " + " ".join(
        f"Token{index}" for index in range(30)
    )

    tokens = fallback_search_tokens(question)

    assert tokens[:4] == ["alpha", "transformer", "beta", "token0"]
    assert len(tokens) == 24
    assert len(tokens) == len(set(tokens))
    assert not {"or", "and", "not"} & set(tokens)
    assert fallback_search_tokens("or AND not") == []


class SelectionLLM:
    def __init__(self, selected_id: uuid.UUID) -> None:
        self.selected_id = selected_id
        self.system = ""
        self.user = ""

    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
    ) -> StructuredT:
        del thinking, stage
        self.system = system
        self.user = user
        if schema is not CandidateSelection:
            raise AssertionError(f"unexpected schema: {schema}")
        return schema.model_validate({"document_ids": [self.selected_id]})


def _candidate(index: int, *, fts_match: bool) -> CandidateDocument:
    document = Document(
        id=uuid.uuid4(),
        sha256=f"{index:064x}",
        original_filename=f"paper-{index}.pdf",
        pdf_object_key=f"pdf/{index}",
        arxiv_id=f"2607.{index:05d}",
        title="T" * 400,
        abstract="A" * 1_500,
        topics=[f"{topic}-" + "X" * 100 for topic in range(10)],
    )
    return CandidateDocument(document=document, fts_match=fts_match)


async def test_candidate_selection_payload_is_bounded_and_carries_match_signal() -> None:
    candidates = [_candidate(index, fts_match=index == 0) for index in range(9)]
    llm = SelectionLLM(candidates[0].document.id)
    router = CorpusRouter(Settings(), llm)

    selected = await router.select("question", candidates)

    assert selected == [candidates[0].document]
    payload_text = llm.user.split("<document_data>\n", 1)[1].split("\n</document_data>", 1)[0]
    payload = json.loads(payload_text)
    assert len(payload) == 9
    assert len(payload[0]["title"]) == 300
    assert len(payload[0]["abstract"]) == 1_200
    assert len(payload[0]["topics"]) == 8
    assert all(len(topic) == 80 for topic in payload[0]["topics"])
    assert payload[0]["fts_match"] is True
    assert payload[1]["fts_match"] is False
    assert "Prefer documents marked fts_match" in llm.system


async def test_candidate_selection_short_circuit_returns_documents_without_llm() -> None:
    candidates = [_candidate(index, fts_match=False) for index in range(8)]
    router = CorpusRouter(Settings(), UnusedLLM())

    selected = await router.select("question", candidates)

    assert selected == [candidate.document for candidate in candidates]


class SufficiencyOnlyLLM:
    def __init__(self) -> None:
        self.schemas: list[type[BaseModel]] = []

    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
    ) -> StructuredT:
        del system, user, thinking, stage
        self.schemas.append(schema)
        if schema is not SufficiencyDecision:
            raise AssertionError("a sole evidence node must be selected without the LLM")
        return schema.model_validate({"sufficient": True})


async def test_single_node_is_selected_without_llm_node_selection(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings()
    llm = SufficiencyOnlyLLM()
    retriever = PageIndexRetriever(settings, MemoryObjectStore(), llm)
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        sha256="a" * 64,
        original_filename="one-page.pdf",
        pdf_object_key="pdf/key",
        title="One page",
    )

    async def artifact(session: object, selected: Document) -> dict[str, object]:
        del session, selected
        return {
            "tree": [
                {
                    "node_id": "node-0000",
                    "title": "One page",
                    "summary": "",
                    "page_start": 1,
                    "page_end": 1,
                }
            ],
            "pages": [{"page": 1, "text": "the exact evidence appears here"}],
        }

    monkeypatch.setattr(retriever, "_artifact", artifact)

    evidence, sufficient, rounds = await retriever.retrieve(
        object(),  # pyright: ignore[reportArgumentType]
        "exact evidence",
        [document],
    )

    assert sufficient is True
    assert rounds == 1
    assert evidence[0].content == "[Page 1]\nthe exact evidence appears here"
    assert llm.schemas == [SufficiencyDecision]
