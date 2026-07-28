#!/usr/bin/env python3
"""Execute a frozen PageIndex evaluation plan and append deterministic scores.

Drives the real GraphRunner in process rather than the HTTP surface, because the frozen
protocol needs per-trial control of the forced route, the oracle document set, and the
evidence budget, and none of those are fields on ChatRequest.

Four of the five reported metrics are computed here from the returned citations. The fifth,
answer_claim_recall, needs a judgement about whether an answer asserts each atomic claim, so
end-to-end trials are appended to a judge queue and scored separately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import get_settings
from vectorless_rag.db import Database, make_engine
from vectorless_rag.evaluation_suite import (
    EvaluationCase,
    EvaluationScore,
    EvaluationTrial,
    FrozenEvaluationManifest,
    load_cases,
)
from vectorless_rag.graph import GraphRunner, GraphRunOptions
from vectorless_rag.llm import DeepSeekLLM, LLMError
from vectorless_rag.models import Document, DocumentStatus, ModelCall, QueryRun, RunRoute
from vectorless_rag.observability import Observability
from vectorless_rag.provider import ProviderGate
from vectorless_rag.retrieval import CorpusRouter, PageIndexRetriever, VectorlessRetriever
from vectorless_rag.schemas import ChatRequest, MetadataConstraints
from vectorless_rag.sql_guard import PostgreSQLAdapter
from vectorless_rag.storage import S3ObjectStore
from vectorless_rag.usage import UsageOwner, usage_context

# Every stratum in this suite is answerable from document text, so the router is correct when
# it chooses documents. Unanswerable cases still route to documents; being unable to answer is
# a retrieval outcome, measured by insufficient rather than by the route.
EXPECTED_ROUTE = RunRoute.documents


def options_for(kind: str, budget: int, oracle: tuple[uuid.UUID, ...]) -> GraphRunOptions:
    """Map an experiment kind onto the graph's frozen-evaluation overrides.

    route leaves the router free so its choice can be scored; the rest pin the route so the
    retrieval stages are measured without routing noise standing in front of them.
    """
    if kind == "route":
        return GraphRunOptions(evidence_token_budget=budget)
    if kind == "oracle_document_retrieval":
        return GraphRunOptions(
            forced_route=EXPECTED_ROUTE,
            oracle_document_ids=oracle,
            evidence_token_budget=budget,
        )
    return GraphRunOptions(forced_route=EXPECTED_ROUTE, evidence_token_budget=budget)


def overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start <= other_end and other_start <= end


def score_citations(case: EvaluationCase, citations: list[dict[str, Any]]) -> dict[str, float]:
    """Score retrieval against the case's target documents and support spans.

    document_recall  - fraction of target documents that appear in the citations.
    support_recall   - fraction of support spans covered by a citation on the same document.
    citation_precision - fraction of citations landing on a target document and a support span.
    """
    targets = {target.arxiv_id for target in case.target_documents}
    spans = [span for support in case.support_sets for span in support.spans]
    documents = [item for item in citations if item.get("source_type") == "document"]

    cited = {str(item["arxiv_id"]) for item in documents if item.get("arxiv_id")}
    document_recall = len(cited & targets) / len(targets) if targets else 0.0

    covered = sum(
        any(
            str(item.get("arxiv_id")) == span.arxiv_id
            and overlaps(
                span.page_start, span.page_end, int(item["page_start"]), int(item["page_end"])
            )
            for item in documents
        )
        for span in spans
    )
    support_recall = covered / len(spans) if spans else 0.0

    useful = sum(
        any(
            str(item.get("arxiv_id")) == span.arxiv_id
            and overlaps(
                span.page_start, span.page_end, int(item["page_start"]), int(item["page_end"])
            )
            for span in spans
        )
        for item in documents
    )
    citation_precision = useful / len(documents) if documents else 0.0
    return {
        "document_recall": document_recall,
        "support_recall": support_recall,
        "citation_precision": citation_precision,
    }


async def token_totals(session: AsyncSession, query_run_id: uuid.UUID) -> tuple[int, int]:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(ModelCall.input_tokens), 0),
                func.coalesce(func.sum(ModelCall.output_tokens), 0),
            ).where(ModelCall.query_run_id == query_run_id)
        )
    ).one()
    return int(row[0]), int(row[1])


class Executor:
    def __init__(self, runner: GraphRunner, documents: dict[str, uuid.UUID]) -> None:
        self.runner = runner
        self.documents = documents

    async def execute(
        self,
        session: AsyncSession,
        case: EvaluationCase,
        trial: EvaluationTrial,
    ) -> tuple[EvaluationScore, dict[str, Any] | None]:
        oracle = tuple(
            self.documents[target.arxiv_id]
            for target in case.target_documents
            if target.arxiv_id in self.documents
        )
        run = QueryRun(
            id=uuid.uuid4(),
            trace_id=uuid.uuid4().hex,
            thread_id=f"eval-{trial.trial_id}",
            api_key_id=None,
            question=case.prompt,
        )
        session.add(run)
        await session.commit()

        started = time.monotonic()
        try:
            with usage_context(UsageOwner(trace_id=run.trace_id, query_run_id=run.id)):
                result = await self.runner.run(
                    session,
                    ChatRequest(
                        message=case.prompt,
                        constraints=MetadataConstraints.model_validate(case.constraints),
                    ),
                    run,
                    options=options_for(trial.experiment_kind, trial.evidence_token_budget, oracle),
                )
        except (LLMError, TimeoutError, ValueError) as error:
            await session.rollback()
            code = getattr(error, "failure_code", type(error).__name__)
            return (
                EvaluationScore(
                    trial_id=trial.trial_id,
                    query_run_id=run.id,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    failure_code=str(code)[:80],
                    included=False,
                ),
                None,
            )

        latency = round((time.monotonic() - started) * 1000)
        citations = result.get("citations", [])
        # Citations carry the document UUID; the suite is keyed by arXiv ID.
        by_id = {str(value): key for key, value in self.documents.items()}
        for item in citations:
            item["arxiv_id"] = by_id.get(str(item.get("source_id")))

        insufficient = bool(result.get("insufficient", False))
        route = str(result.get("route", ""))
        if case.answerable:
            route_correct = float(route == EXPECTED_ROUTE.value)
        else:
            # A correct refusal is the whole measurement for an unanswerable case.
            route_correct = float(insufficient and not citations)

        metrics = (
            score_citations(case, citations)
            if case.answerable
            else {
                "document_recall": float(not citations),
                "support_recall": float(not citations),
                "citation_precision": float(not citations),
            }
        )
        inputs, outputs = await token_totals(session, run.id)
        score = EvaluationScore(
            trial_id=trial.trial_id,
            query_run_id=run.id,
            route_correct=route_correct,
            document_recall=metrics["document_recall"],
            support_recall=metrics["support_recall"],
            citation_precision=metrics["citation_precision"],
            latency_ms=latency,
            input_tokens=inputs,
            output_tokens=outputs,
        )
        judge = None
        if trial.experiment_kind == "forced_route_end_to_end" and case.answerable:
            judge = {
                "trial_id": str(trial.trial_id),
                "item_id": str(case.item_id),
                "claims": [claim.claim for claim in case.atomic_claims],
                "answer": str(result.get("answer", "")),
            }
        return score, judge


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required to execute trials")
    manifest = FrozenEvaluationManifest.model_validate_json(
        (args.run / "manifest.json").read_text(encoding="utf-8")
    )
    trials = [
        EvaluationTrial.model_validate_json(line)
        for line in (args.run / "trials.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = {case.item_id: case for case in load_cases(args.dataset)}
    scores_path = args.run / "scores.jsonl"
    done = {
        uuid.UUID(cast(dict[str, Any], json.loads(line))["trial_id"])
        for line in scores_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    pending = [trial for trial in trials if trial.trial_id not in done]
    if args.limit:
        pending = pending[: args.limit]
    print(
        json.dumps(
            {
                "experiment_id": str(manifest.experiment_id),
                "planned": len(trials),
                "already_scored": len(done),
                "this_run": len(pending),
            },
            indent=2,
        )
    )
    if not pending:
        return

    database = Database(settings)
    sql_engine = make_engine(settings.sql_database_url, pool_size=5)
    store = S3ObjectStore(settings)
    observability = Observability(settings)
    provider_gate = ProviderGate(settings)
    llm = DeepSeekLLM(settings, observability, provider_gate=provider_gate)
    try:
        async with database.session() as session:
            rows = (
                await session.execute(
                    select(Document.arxiv_id, Document.id).where(
                        Document.status == DocumentStatus.ready,
                        Document.active_index_version_id.is_not(None),
                    )
                )
            ).all()
        documents = {str(row[0]): row[1] for row in rows}
        missing = sorted(
            {
                target.arxiv_id
                for trial in pending
                for target in cases[trial.item_id].target_documents
                if target.arxiv_id not in documents
            }
        )
        if missing:
            raise SystemExit(
                f"{len(missing)} target document(s) are not indexed and ready: {missing[:5]}"
            )
        print(f"resolved {len(documents)} indexed documents; all trial targets present")
        if args.dry_run:
            for trial in pending[:5]:
                case = cases[trial.item_id]
                print(
                    f"  {trial.experiment_kind:26s} budget={trial.evidence_token_budget:<6d} "
                    f"{case.stratum} {str(trial.trial_id)[:8]}"
                )
            print("dry run: no provider calls issued")
            return

        async with AsyncExitStack() as stack:
            checkpointer = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(settings.database_url.replace("+asyncpg", ""))
            )
            await checkpointer.setup()
            router = CorpusRouter(settings, llm)
            pageindex = PageIndexRetriever(settings, store, llm)
            runner = GraphRunner(
                settings,
                llm,
                VectorlessRetriever(settings, router, pageindex),
                PostgreSQLAdapter(sql_engine, settings),
                observability,
                checkpointer,
            )
            executor = Executor(runner, documents)
            judge_path = args.run / "judge-queue.jsonl"
            for index, trial in enumerate(pending, 1):
                async with database.session() as session:
                    score, judge = await executor.execute(session, cases[trial.item_id], trial)
                with scores_path.open("a", encoding="utf-8") as handle:
                    handle.write(score.model_dump_json() + "\n")
                if judge is not None:
                    with judge_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(judge, ensure_ascii=False) + "\n")
                flag = "" if score.included else f" FAILED {score.failure_code}"
                print(
                    f"[{index}/{len(pending)}] {trial.experiment_kind} {score.latency_ms}ms{flag}"
                )
    finally:
        await sql_engine.dispose()
        await database.close()


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "evaluation"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=root / "run")
    parser.add_argument("--dataset", type=Path, default=root / "pageindex-v2-regression.jsonl")
    parser.add_argument("--limit", type=int, default=0, help="execute at most this many trials")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve documents and print the plan without calling the provider",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
