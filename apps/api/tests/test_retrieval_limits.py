import uuid
from typing import TypeVar

from pydantic import BaseModel

from vectorless_rag.config import Settings
from vectorless_rag.models import Document
from vectorless_rag.retrieval import PageIndexRetriever
from vectorless_rag.schemas import NodeChoice, NodeSelection
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
