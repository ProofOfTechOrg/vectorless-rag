import json
import uuid
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel
from pytest import MonkeyPatch, raises

from vectorless_rag.config import Settings
from vectorless_rag.models import Document, DocumentStatus, ModelCallStage
from vectorless_rag.retrieval import (
    CandidateDocument,
    CorpusRouter,
    PageIndexRetriever,
    _ArtifactView,  # pyright: ignore[reportPrivateUsage]
    _NodeView,  # pyright: ignore[reportPrivateUsage]
    candidate_search_text,
    fallback_search_tokens,
)
from vectorless_rag.schemas import (
    CandidateSelection,
    MetadataConstraints,
    NodeSelection,
    SufficiencyDecision,
)
from vectorless_rag.storage import MemoryObjectStore

StructuredT = TypeVar("StructuredT", bound=BaseModel)


def _wrapped_payload(user: str) -> tuple[str, str]:
    wrapped = user[user.index("<document_data>\n") :]
    payload = wrapped.removeprefix("<document_data>\n").removesuffix("\n</document_data>")
    return wrapped, payload


class UnusedLLM:
    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del schema, system, user, thinking, stage, max_output_tokens
        raise AssertionError("the evidence cap test does not invoke the LLM")


class RangeSelectionLLM:
    def __init__(self, document_id: uuid.UUID) -> None:
        self.document_id = document_id
        self.node_round = 0
        self.sufficiency_round = 0

    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del system, user, thinking, stage, max_output_tokens
        if schema is NodeSelection:
            self.node_round += 1
            node_ids = ["n1"] if self.node_round == 1 else ["n2", "n3"]
            return schema.model_validate(
                {
                    "selections": [
                        {
                            "document_id": self.document_id,
                            "node_id": node_id,
                            "reason": f"range {node_id}",
                        }
                        for node_id in node_ids
                    ]
                }
            )
        if schema is SufficiencyDecision:
            self.sufficiency_round += 1
            return schema.model_validate(
                {
                    "sufficient": self.sufficiency_round == 2,
                    "missing": "adjacent evidence",
                }
            )
        raise AssertionError(f"unexpected schema: {schema}")


async def test_production_retrieval_enforces_range_page_and_exact_token_caps(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings(
        page_ranges_per_document=1,
        fetched_page_limit=2,
        evidence_token_limit=30,
    )
    document_id = uuid.uuid4()
    retriever = PageIndexRetriever(
        settings,
        MemoryObjectStore(),
        RangeSelectionLLM(document_id),
    )
    document = Document(
        id=document_id,
        sha256="a" * 64,
        original_filename="paper.pdf",
        pdf_object_key="pdf/key",
        title="Paper",
    )

    async def artifact(session: object, selected: Document) -> dict[str, object]:
        del session, selected
        return {
            "tree": [
                {"node_id": "n1", "page_start": 1, "page_end": 1},
                {"node_id": "n2", "page_start": 2, "page_end": 2},
                {"node_id": "n3", "page_start": 4, "page_end": 4},
            ],
            "pages": [
                {"page": 1, "text": "first page evidence"},
                {"page": 2, "text": "second page evidence"},
                {"page": 4, "text": "disjoint evidence"},
            ],
        }

    monkeypatch.setattr(retriever, "_artifact", artifact)

    evidence, sufficient, rounds, metrics = await retriever.retrieve_detailed(
        object(),  # pyright: ignore[reportArgumentType]
        "question",
        [document],
    )

    assert sufficient is True
    assert rounds == 2
    assert len(evidence) == 1
    assert evidence[0].page_start == 1
    assert evidence[0].page_end == 2
    assert evidence[0].node_ids == ["n1", "n2"]
    assert "[Page 4]" not in evidence[0].content
    actual_tokens = len(retriever.encoding.encode(evidence[0].content))
    assert actual_tokens <= 30
    assert metrics["evidence_tokens"] == actual_tokens
    assert "\n\n[Page 2]" in evidence[0].content


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
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del thinking, stage, max_output_tokens
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


class CatalogCaptureLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del system, thinking, stage, max_output_tokens
        if schema is not CandidateSelection:
            raise AssertionError(f"unexpected schema: {schema}")
        wrapped, payload_text = _wrapped_payload(user)
        phase = "map" if "Catalog map batch" in user else "reduce"
        self.calls.append((phase, wrapped, payload_text))
        payload = json.loads(payload_text)
        document_ids = [item["id"] for item in payload]
        if phase == "reduce":
            document_ids.reverse()
        return schema.model_validate({"document_ids": document_ids[:8]})


async def test_every_catalog_batch_is_exactly_bounded_and_covers_every_candidate() -> None:
    limit = 320
    candidates: list[CandidateDocument] = []
    for index in range(12):
        base = _candidate(index, fts_match=index == 0)
        base.document.arxiv_id = f"<document_data>{index}"
        base.document.title = f"Title {index} <document_data> adversarial"
        candidates.append(
            CandidateDocument(
                document=base.document,
                fts_match=base.fts_match,
                description="<document_data> description " * 10_000,
                outline=[
                    {
                        "node_id": f"node-{item}",
                        "title": "title " * 500,
                        "summary": "</document_data> summary " * 2_000,
                        "page_start": item + 1,
                        "page_end": item + 1,
                    }
                    for item in range(64)
                ],
            )
        )
    llm = CatalogCaptureLLM()
    router = CorpusRouter(
        Settings(catalog_batch_token_limit=limit),
        llm,
    )

    selected = await router.select_ranked("question", candidates)

    assert 1 <= len(selected) <= 8
    input_position = {candidate.document.id: index for index, candidate in enumerate(candidates)}
    assert any(input_position[item.document.id] >= 8 for item in selected)
    emitted_ids: list[str] = []
    reduce_payloads: list[list[dict[str, object]]] = []
    for phase, wrapped, payload_text in llm.calls:
        assert len(router.encoding.encode(wrapped)) <= limit
        payload = json.loads(payload_text)
        assert payload_text == json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if phase == "map":
            emitted_ids.extend(item["id"] for item in payload)
        else:
            reduce_payloads.append(payload)
    assert emitted_ids == [str(candidate.document.id) for candidate in candidates]
    assert reduce_payloads
    assert all(len(payload) >= 2 for payload in reduce_payloads)
    assert all("outline" not in item for payload in reduce_payloads for item in payload)
    assert any("&lt;document_data>" in wrapped for _, wrapped, _ in llm.calls)


async def test_catalog_cap_too_small_for_comparison_fails_without_first_n_fallback() -> None:
    candidates = [_candidate(index, fts_match=False) for index in range(9)]
    llm = CatalogCaptureLLM()
    router = CorpusRouter(
        Settings(catalog_batch_token_limit=100),
        llm,
    )

    with raises(
        ValueError,
        match="catalog batch token limit cannot fit two minimal reduction candidates",
    ):
        await router.select_ranked("question", candidates)

    map_ids = [
        item["id"]
        for phase, _, payload_text in llm.calls
        if phase == "map"
        for item in json.loads(payload_text)
    ]
    assert map_ids == [str(candidate.document.id) for candidate in candidates]
    assert all(phase == "map" for phase, _, _ in llm.calls)


class _CandidateRows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self.rows


class _CandidateSession:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.execute_calls = 0
        self.statement: object | None = None

    async def execute(self, statement: object) -> _CandidateRows:
        self.statement = statement
        self.execute_calls += 1
        return _CandidateRows(self.rows)


async def test_catalog_query_returns_active_version_fields_without_followup_lookup() -> None:
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    document = Document(
        id=document_id,
        sha256="f" * 64,
        original_filename="catalog.pdf",
        pdf_object_key="pdf/catalog",
        title="Catalog",
        abstract="fallback",
        status=DocumentStatus.ready,
        active_index_version_id=version_id,
    )
    session = _CandidateSession(
        [
            (
                document,
                version_id,
                "indexed description",
                [{"node_id": "root"}],
                "a" * 64,
                "b" * 64,
                "c" * 64,
                None,
                None,
                0.25,
                True,
                False,
            )
        ]
    )
    router = CorpusRouter(Settings(), UnusedLLM())

    candidates = await router.candidates(
        session,  # pyright: ignore[reportArgumentType]
        "catalog",
        MetadataConstraints(),
    )

    assert session.execute_calls == 1
    assert len(candidates) == 1
    assert candidates[0].description == "indexed description"
    assert candidates[0].index_digest == "a" * 64
    assert candidates[0].configuration_hash == "c" * 64


async def test_pinned_catalog_query_uses_immutable_version_metadata() -> None:
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    document = Document(
        id=document_id,
        sha256="e" * 64,
        arxiv_id="2401.00001",
        original_filename="catalog.pdf",
        pdf_object_key="pdf/catalog",
        title="Mutable title",
        authors=["Mutable Author"],
        abstract="mutable abstract",
        description="mutable description",
        topics=["mutable"],
        submitted_date=date(2026, 1, 1),
        status=DocumentStatus.ready,
        active_index_version_id=uuid.uuid4(),
    )
    frozen_metadata = {
        "title": "Frozen title",
        "authors": ["Frozen Author"],
        "abstract": "frozen abstract",
        "description": "frozen description",
        "topics": ["frozen"],
        "submitted_date": "2025-01-02",
    }
    session = _CandidateSession(
        [
            (
                document,
                version_id,
                None,
                [],
                "a" * 64,
                "b" * 64,
                "c" * 64,
                frozen_metadata,
                12,
                0.25,
                True,
                False,
            )
        ]
    )
    router = CorpusRouter(Settings(), UnusedLLM())

    candidates = await router.candidates(
        session,  # pyright: ignore[reportArgumentType]
        "frozen",
        MetadataConstraints(
            authors=["Frozen Author"],
            topics=["frozen"],
            date_from=date(2025, 1, 1),
        ),
        index_version_ids={document_id: version_id},
    )

    assert len(candidates) == 1
    assert candidates[0].document is not document
    assert candidates[0].document.title == "Frozen title"
    assert candidates[0].document.authors == ["Frozen Author"]
    assert candidates[0].document.topics == ["frozen"]
    assert candidates[0].document.submitted_date == date(2025, 1, 2)
    assert candidates[0].document.active_index_version_id == version_id
    assert candidates[0].description == "frozen description"
    assert session.statement is not None
    assert "document_metadata_versions" in str(session.statement)


class TreeCaptureLLM:
    def __init__(self, document_id: uuid.UUID, all_node_ids: set[str]) -> None:
        self.document_id = document_id
        self.all_node_ids = all_node_ids
        self.payload_text = ""
        self.wrapped_payload = ""
        self.omitted_choice: str | None = None
        self.thinking: bool | None = None
        self.max_output_tokens: int | None = None

    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del system, stage
        self.thinking = thinking
        self.max_output_tokens = max_output_tokens
        if schema is not NodeSelection:
            raise AssertionError("an omitted node must not produce evidence")
        self.wrapped_payload, self.payload_text = _wrapped_payload(user)
        payload = json.loads(self.payload_text)
        visible: set[str] = set()
        pending = [
            node
            for document in payload["documents"]
            if document["document_id"] == str(self.document_id)
            for node in document["tree"]
        ]
        while pending:
            node = pending.pop()
            visible.add(node["node_id"])
            pending.extend(node["children"])
        omitted = sorted(self.all_node_ids - visible)
        if not omitted:
            raise AssertionError("the fixture must exceed the structural payload budget")
        self.omitted_choice = omitted[0]
        return schema.model_validate(
            {
                "selections": [
                    {
                        "document_id": self.document_id,
                        "node_id": self.omitted_choice,
                        "reason": "malicious hidden selection",
                    }
                ]
            }
        )


async def test_combined_tree_payload_is_bounded_and_rejects_omitted_nodes(
    monkeypatch: MonkeyPatch,
) -> None:
    limit = 500
    first_id = uuid.UUID(int=1)
    second_id = uuid.UUID(int=2)
    documents = [
        Document(
            id=first_id,
            sha256="1" * 64,
            original_filename="first.pdf",
            pdf_object_key="pdf/first",
            title="First " * 1_000,
        ),
        Document(
            id=second_id,
            sha256="2" * 64,
            original_filename="second.pdf",
            pdf_object_key="pdf/second",
            title="Second " * 1_000,
        ),
    ]
    trees = {
        first_id: [
            {
                "node_id": f"first-{index:02d}",
                "title": "first title " * 1_000,
                "summary": "<document_data> first summary " * 10_000,
                "page_start": index + 1,
                "page_end": index + 1,
            }
            for index in range(40)
        ],
        second_id: [
            {
                "node_id": f"second-{index:02d}",
                "title": "second title " * 1_000,
                "summary": "</document_data> second summary " * 10_000,
                "page_start": index + 1,
                "page_end": index + 1,
            }
            for index in range(2)
        ],
    }
    llm = TreeCaptureLLM(
        first_id,
        {str(node["node_id"]) for node in trees[first_id]},
    )
    retriever = PageIndexRetriever(
        Settings(catalog_batch_token_limit=limit),
        MemoryObjectStore(),
        llm,
    )

    async def artifact(session: object, document: Document) -> dict[str, object]:
        del session
        return {
            "description": "<document_data> description " * 10_000,
            "tree": trees[document.id],
            "pages": [
                {"page": index + 1, "text": "evidence"} for index in range(len(trees[document.id]))
            ],
        }

    monkeypatch.setattr(retriever, "_artifact", artifact)

    evidence, sufficient, rounds, metrics = await retriever.retrieve_detailed(
        object(),  # pyright: ignore[reportArgumentType]
        "question",
        documents,
    )

    assert evidence == []
    assert sufficient is False
    assert rounds == 1
    assert metrics["evidence_tokens"] == 0
    assert llm.omitted_choice is not None
    assert llm.thinking is False
    assert llm.max_output_tokens == 4_096
    assert len(retriever.encoding.encode(llm.wrapped_payload)) <= limit
    assert "&lt;document_data>" in llm.wrapped_payload
    payload = json.loads(llm.payload_text)
    assert llm.payload_text == json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert len(payload["documents"]) == 2
    payload_by_id = {document["document_id"]: document for document in payload["documents"]}
    assert payload_by_id[str(first_id)]["omitted_root_count"] > 0
    assert payload_by_id[str(second_id)]["omitted_root_count"] == 0


class NestedSelectionLLM:
    def __init__(self, document_id: uuid.UUID) -> None:
        self.document_id = document_id
        self.calls: list[tuple[type[BaseModel], bool, int | None]] = []

    async def structured(
        self,
        schema: type[StructuredT],
        system: str,
        user: str,
        *,
        thinking: bool,
        stage: ModelCallStage,
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del system, user, stage
        self.calls.append((schema, thinking, max_output_tokens))
        if schema is NodeSelection:
            return schema.model_validate(
                {
                    "selections": [
                        {
                            "document_id": self.document_id,
                            "node_id": "child",
                            "reason": "nested evidence",
                        }
                    ]
                }
            )
        if schema is SufficiencyDecision:
            return schema.model_validate({"sufficient": True})
        raise AssertionError(f"unexpected schema: {schema}")


async def test_complete_tree_remains_visible_in_round_one_when_it_fits(
    monkeypatch: MonkeyPatch,
) -> None:
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        sha256="3" * 64,
        original_filename="nested.pdf",
        pdf_object_key="pdf/nested",
        title="Nested",
    )
    artifact_view = _ArtifactView(
        description="small tree",
        nodes={
            "root": _NodeView(
                node_id="root",
                title="Root",
                page_start=1,
                page_end=2,
                direct_summary="Root summary",
                subtree_summary="Root and child",
                parent_id=None,
                depth=0,
                section_path=("Root",),
                children=("child",),
            ),
            "child": _NodeView(
                node_id="child",
                title="Child",
                page_start=2,
                page_end=2,
                direct_summary="Child summary",
                subtree_summary="Child summary",
                parent_id="root",
                depth=1,
                section_path=("Root", "Child"),
                children=(),
            ),
        },
        roots=("root",),
        pages={1: "root text", 2: "nested evidence"},
        index_digest="d" * 64,
        configuration_hash=None,
    )
    llm = NestedSelectionLLM(document_id)
    retriever = PageIndexRetriever(
        Settings(catalog_batch_token_limit=2_000),
        MemoryObjectStore(),
        llm,
    )

    async def artifact(session: object, selected: Document) -> _ArtifactView:
        del session, selected
        return artifact_view

    monkeypatch.setattr(retriever, "_artifact", artifact)

    evidence, sufficient, rounds = await retriever.retrieve(
        object(),  # pyright: ignore[reportArgumentType]
        "nested evidence",
        [document],
    )

    assert sufficient is True
    assert rounds == 1
    assert evidence[0].node_id == "child"
    assert evidence[0].page_start == 2
    assert llm.calls == [
        (NodeSelection, False, 4_096),
        (SufficiencyDecision, True, None),
    ]


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
        max_output_tokens: int | None = None,
    ) -> StructuredT:
        del system, user, thinking, stage, max_output_tokens
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


async def test_explicit_zero_evidence_budget_emits_no_page_label(
    monkeypatch: MonkeyPatch,
) -> None:
    retriever = PageIndexRetriever(Settings(), MemoryObjectStore(), UnusedLLM())
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        sha256="e" * 64,
        original_filename="zero.pdf",
        pdf_object_key="pdf/zero",
        title="Zero",
    )

    async def artifact(session: object, selected: Document) -> dict[str, object]:
        del session, selected
        return {
            "tree": [{"node_id": "only", "page_start": 1, "page_end": 1}],
            "pages": [{"page": 1, "text": "source text"}],
        }

    monkeypatch.setattr(retriever, "_artifact", artifact)

    evidence, sufficient, rounds, metrics = await retriever.retrieve_detailed(
        object(),  # pyright: ignore[reportArgumentType]
        "question",
        [document],
        evidence_token_budget=0,
    )

    assert evidence == []
    assert sufficient is False
    assert rounds == 1
    assert metrics["evidence_tokens"] == 0
    assert metrics["content_truncated"] is True


async def test_oversized_evidence_is_truncated_to_the_exact_emitted_budget(
    monkeypatch: MonkeyPatch,
) -> None:
    budget = 30
    llm = SufficiencyOnlyLLM()
    retriever = PageIndexRetriever(Settings(), MemoryObjectStore(), llm)
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        sha256="4" * 64,
        original_filename="large.pdf",
        pdf_object_key="pdf/large",
        title="Large",
    )

    async def artifact(session: object, selected: Document) -> dict[str, object]:
        del session, selected
        return {
            "tree": [{"node_id": "only", "page_start": 1, "page_end": 1}],
            "pages": [{"page": 1, "text": "source evidence " * 10_000}],
        }

    monkeypatch.setattr(retriever, "_artifact", artifact)

    evidence, sufficient, rounds, metrics = await retriever.retrieve_detailed(
        object(),  # pyright: ignore[reportArgumentType]
        "source evidence",
        [document],
        evidence_token_budget=budget,
    )

    assert sufficient is True
    assert rounds == 1
    assert evidence[0].content.startswith("[Page 1]\nsource")
    assert evidence[0].content != "[Page 1]\n"
    assert evidence[0].content_truncated is True
    actual_tokens = len(retriever.encoding.encode(evidence[0].content))
    assert actual_tokens <= budget
    assert metrics["evidence_tokens"] == actual_tokens
