from __future__ import annotations

import json
import uuid
from typing import Any, NotRequired, Required, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings
from vectorless_rag.llm import UNTRUSTED_DOCUMENT_SYSTEM, StructuredLLM, wrap_untrusted_document
from vectorless_rag.models import QueryRun, RetrievalAudit, RunRoute, SqlAudit
from vectorless_rag.observability import Observability
from vectorless_rag.retrieval import RetrievalResult, VectorlessRetriever
from vectorless_rag.schemas import (
    ChatRequest,
    Citation,
    Evidence,
    RouteDecision,
    SQLPlan,
    SynthesisResult,
)
from vectorless_rag.sql_guard import DatabaseAdapter, SQLRejected, SQLResult, validate_sql


class GraphState(TypedDict):
    question: Required[str]
    route: NotRequired[str]
    route_reason: NotRequired[str]
    clarification: NotRequired[str]
    evidence: NotRequired[list[dict[str, Any]]]
    retrieval: NotRequired[dict[str, Any]]
    sql_rows: NotRequired[list[dict[str, Any]]]
    sql_audit_id: NotRequired[str]
    sql_document_ids: NotRequired[list[str]]
    answer: NotRequired[str]
    citations: NotRequired[list[dict[str, Any]]]
    insufficient: NotRequired[bool]


class GraphUpdate(TypedDict, total=False):
    route: str
    route_reason: str
    clarification: str
    evidence: list[dict[str, Any]]
    retrieval: dict[str, Any]
    sql_rows: list[dict[str, Any]]
    sql_audit_id: str
    sql_document_ids: list[str]
    answer: str
    citations: list[dict[str, Any]]
    insufficient: bool


class GraphResult(TypedDict):
    route: str
    answer: str
    citations: list[dict[str, Any]]
    insufficient: bool
    sql_audit_id: NotRequired[str]


def _selected_route(state: GraphState) -> str:
    selected = state.get("route")
    if selected is None:
        raise RuntimeError("graph state has no selected route")
    return selected


class GraphRunner:
    def __init__(
        self,
        settings: Settings,
        llm: StructuredLLM,
        retriever: VectorlessRetriever,
        sql_adapter: DatabaseAdapter,
        observability: Observability,
        checkpointer: Any = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.retriever = retriever
        self.sql_adapter = sql_adapter
        self.observability = observability
        self.checkpointer = checkpointer

    async def run(
        self,
        session: AsyncSession,
        request: ChatRequest,
        run: QueryRun,
    ) -> GraphResult:
        builder: StateGraph[GraphState] = StateGraph(GraphState)

        async def route(state: GraphState) -> GraphUpdate:
            decision = await self.llm.structured(
                RouteDecision,
                """Route the request: sql for catalog counts/lists/status, documents for paper
content, hybrid when catalog filtering must identify papers before content retrieval, or clarify
when essential intent is missing. Do not answer the question.""",
                state["question"],
                thinking=False,
            )
            return {
                "route": decision.route.value,
                "route_reason": decision.reason,
                "clarification": decision.clarification_question or "",
            }

        async def sql_node(state: GraphState) -> GraphUpdate:
            result, audit_id = await self._run_sql(session, run, state["question"])
            document_ids: list[str] = []
            for row in result.rows:
                raw_id = row.get("document_id") or row.get("id")
                try:
                    document_ids.append(str(uuid.UUID(str(raw_id))))
                except (ValueError, TypeError, AttributeError):
                    continue
            evidence = Evidence(
                source_type="sql",
                source_id=str(audit_id),
                title="Paper catalog query",
                content=json.dumps(result.rows, default=str),
                retrieval_reason="Guarded query over curated catalog views",
            )
            return {
                "sql_rows": result.rows,
                "sql_audit_id": str(audit_id),
                "sql_document_ids": document_ids,
                "evidence": [evidence.model_dump(mode="json")],
            }

        async def documents_node(state: GraphState) -> GraphUpdate:
            restrict = [uuid.UUID(value) for value in state.get("sql_document_ids", [])]
            result = await self.retriever.retrieve(
                session,
                state["question"],
                request.constraints,
                restrict_ids=restrict or None,
            )
            await self._audit_retrieval(session, run, result)
            existing = (
                state.get("evidence", []) if _selected_route(state) == RunRoute.hybrid.value else []
            )
            return {
                "evidence": existing + [item.model_dump(mode="json") for item in result.evidence],
                "retrieval": {
                    "candidate_ids": [str(item) for item in result.candidate_ids],
                    "selected_ids": [str(item) for item in result.selected_ids],
                    "sufficient": result.sufficient,
                    "rounds": result.rounds,
                },
                "insufficient": not result.sufficient,
            }

        async def synthesize(state: GraphState) -> GraphUpdate:
            selected_route = _selected_route(state)
            if selected_route == RunRoute.clarify.value:
                return {
                    "answer": state.get("clarification") or "Please clarify what you want to know.",
                    "citations": [],
                    "insufficient": True,
                }
            evidence = TypeAdapter(list[Evidence]).validate_python(state.get("evidence", []))
            if not evidence or (
                selected_route in {RunRoute.documents.value, RunRoute.hybrid.value}
                and state.get("insufficient", False)
            ):
                return {
                    "answer": (
                        "I could not find sufficient evidence in the indexed corpus "
                        "to answer safely."
                    ),
                    "citations": [],
                    "insufficient": True,
                }
            labels = {f"E{index}": item for index, item in enumerate(evidence, 1)}
            evidence_text = "\n\n".join(
                f"[{label}] {item.title}; pages {item.page_start}-{item.page_end}\n{item.content}"
                for label, item in labels.items()
            )
            synthesis = await self.llm.structured(
                SynthesisResult,
                UNTRUSTED_DOCUMENT_SYSTEM
                + "\nAnswer only from the evidence. Attach [E#] to every substantive claim. "
                "If evidence is insufficient, explicitly refuse.",
                f"Question: {state['question']}\n{wrap_untrusted_document(evidence_text)}",
                thinking=True,
            )
            cited_labels = list(dict.fromkeys(synthesis.citation_ids))
            if any(label not in labels for label in cited_labels):
                return {
                    "answer": "I could not produce citations that resolve to retrieved evidence.",
                    "citations": [],
                    "insufficient": True,
                }
            if not synthesis.insufficient and any(
                f"[{label}]" not in synthesis.answer for label in cited_labels
            ):
                return {
                    "answer": "I could not produce an answer with inline evidence citations.",
                    "citations": [],
                    "insufficient": True,
                }
            citations: list[Citation] = []
            for label in cited_labels:
                item = labels[label]
                if item.source_type == "document":
                    citations.append(
                        Citation(
                            source_type="document",
                            source_id=item.source_id,
                            title=item.title,
                            node_id=item.node_id,
                            page_start=item.page_start,
                            page_end=item.page_end,
                            retrieval_reason=item.retrieval_reason,
                        )
                    )
                else:
                    sql_audit_id = state.get("sql_audit_id")
                    if sql_audit_id is None:
                        raise RuntimeError("SQL evidence has no audit identifier")
                    citations.append(
                        Citation(
                            source_type="sql",
                            source_id=item.source_id,
                            title=item.title,
                            sql_audit_id=uuid.UUID(sql_audit_id),
                        )
                    )
            if not citations and not synthesis.insufficient:
                return {
                    "answer": "I could not produce a grounded answer with resolvable citations.",
                    "citations": [],
                    "insufficient": True,
                }
            return {
                "answer": synthesis.answer,
                "citations": [item.model_dump(mode="json") for item in citations],
                "insufficient": synthesis.insufficient,
            }

        def after_route(state: GraphState) -> str:
            return _selected_route(state)

        builder.add_node("route", route)
        builder.add_node("sql", sql_node)
        builder.add_node("documents", documents_node)
        builder.add_node("synthesize", synthesize)
        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            after_route,
            {
                RunRoute.sql.value: "sql",
                RunRoute.documents.value: "documents",
                RunRoute.hybrid.value: "sql",
                RunRoute.clarify.value: "synthesize",
            },
        )

        def after_sql(state: GraphState) -> str:
            return "documents" if _selected_route(state) == RunRoute.hybrid.value else "synthesize"

        builder.add_conditional_edges(
            "sql", after_sql, {"documents": "documents", "synthesize": "synthesize"}
        )
        builder.add_edge("documents", "synthesize")
        builder.add_edge("synthesize", END)
        graph = builder.compile(checkpointer=self.checkpointer)
        config = {"configurable": {"thread_id": run.thread_id}}
        with self.observability.span(
            "rag-request",
            run.trace_id,
            input={"question": request.message},
            metadata={"session_id": run.thread_id, "release": self.settings.release},
        ):
            initial: GraphState = {"question": request.message}
            result = await cast(Any, graph).ainvoke(initial, config=cast(Any, config))
        return TypeAdapter(GraphResult).validate_python(result)

    async def _run_sql(
        self, session: AsyncSession, run: QueryRun, question: str
    ) -> tuple[SQLResult, uuid.UUID]:
        error: str | None = None
        for attempt in range(2):
            prompt = (
                "Generate one PostgreSQL SELECT/CTE over only agent.paper_catalog_v1 or "
                "agent.ingestion_summary_v1. Use document_id when the papers will be retrieved. "
                "Never use comments."
            )
            if error:
                prompt += f" The prior query was rejected: {error}. Repair it once."
            plan = await self.llm.structured(SQLPlan, prompt, question, thinking=True)
            audit = SqlAudit(run_id=run.id, generated_sql=plan.sql, allowed=False)
            session.add(audit)
            await session.flush()
            try:
                validated = validate_sql(plan.sql, self.settings.sql_row_limit)
                audit.normalized_sql = validated.normalized
                result = await self.sql_adapter.execute(validated)
                audit.allowed = True
                audit.explain_cost = result.explain_cost
                audit.row_count = len(result.rows)
                audit.payload_bytes = result.payload_bytes
                audit.result_hash = result.result_hash
                return result, audit.id
            except SQLRejected as exc:
                error = str(exc)
                audit.rejection_reason = error
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    async def _audit_retrieval(
        session: AsyncSession, run: QueryRun, result: RetrievalResult
    ) -> None:
        locators = [
            {
                "source_id": item.source_id,
                "node_id": item.node_id,
                "page_start": item.page_start,
                "page_end": item.page_end,
            }
            for item in result.evidence
        ]
        session.add(
            RetrievalAudit(
                run_id=run.id,
                candidate_document_ids=result.candidate_ids,
                selected_document_ids=result.selected_ids,
                locators=locators,
                sufficient=result.sufficient,
                rounds=result.rounds,
                evidence_bytes=sum(len(item.content.encode()) for item in result.evidence),
                evidence_hash=result.evidence_hash,
            )
        )
