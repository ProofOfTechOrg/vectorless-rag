import uuid
from typing import TypeVar

from pydantic import BaseModel
from pytest import MonkeyPatch

from vectorless_rag.config import Settings
from vectorless_rag.models import Document
from vectorless_rag.retrieval import PageIndexRetriever, candidate_search_text
from vectorless_rag.schemas import NodeChoice, NodeSelection, SufficiencyDecision
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
    ) -> StructuredT:
        del schema, system, user, thinking
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
    ) -> StructuredT:
        del system, user, thinking
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
