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
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings, get_settings
from vectorless_rag.db import Database, driver_free_url, make_engine
from vectorless_rag.evaluation_suite import (
    EvaluationCase,
    EvaluationScore,
    EvaluationTrial,
    FrozenEvaluationManifest,
    load_cases,
)
from vectorless_rag.graph import GraphResult, GraphRunner, GraphRunOptions
from vectorless_rag.llm import DeepSeekLLM, LLMError
from vectorless_rag.models import (
    Document,
    DocumentStatus,
    Experiment,
    ExperimentMembership,
    ModelCall,
    QueryRun,
    RunFailureCode,
    RunRoute,
)
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
    def __init__(
        self,
        runner: GraphRunner,
        documents: dict[str, uuid.UUID],
        settings: Settings,
        experiment_id: uuid.UUID,
    ) -> None:
        self.runner = runner
        self.documents = documents
        self.settings = settings
        self.experiment_id = experiment_id

    async def finalize(
        self,
        session: AsyncSession,
        run: QueryRun,
        *,
        latency: int,
        result: GraphResult | None = None,
        failure_code: RunFailureCode | None = None,
    ) -> None:
        """Terminalize the QueryRun the way the HTTP surface does.

        query_run_status reads answer and failure_code; a row carrying neither stays
        `running` forever and makes query-cost-report treat the whole window as incomplete.
        The precise provider code stays on the score, which is free text; this column only
        carries the coarse category the serving report groups by.
        """
        run.latency_ms = latency
        if result is not None:
            run.route = RunRoute(result["route"])
            run.answer = result["answer"]
            # Copied because scoring annotates the same citation dicts with an arXiv ID that
            # belongs to the evaluation, not to the stored run.
            run.citations = [dict(item) for item in result.get("citations", [])]
            run.insufficient = bool(result.get("insufficient", False))
        else:
            run.failure_code = failure_code or RunFailureCode.request_failed
            run.insufficient = True
        await session.commit()

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
        # The cost ceiling is enforced per model call by resolving the call's owning run to an
        # experiment through this row. Without it the reservation finds no experiment and the
        # cap never fires, which is how a 1,440-trial run ends up unbounded.
        session.add(
            ExperimentMembership(
                experiment_id=self.experiment_id,
                query_run_id=run.id,
                trial_key=str(trial.trial_id),
                repetition=trial.repetition,
                sequence_number=trial.sequence_number,
                included=True,
            )
        )
        await session.commit()

        # Built outside the try so a malformed case raises instead of being recorded as an
        # excluded trial: a bad dataset is a defect, not a provider failure.
        request = ChatRequest(
            message=case.prompt,
            constraints=MetadataConstraints.model_validate(case.constraints),
        )
        options = options_for(trial.experiment_kind, trial.evidence_token_budget, oracle)

        started = time.monotonic()
        try:
            # The same deadline the HTTP surface enforces. Without it this scores as a clean
            # success exactly the long multi-round runs that production returns as 504, which
            # would bias the report toward the hardest cases the suite exists to measure.
            async with asyncio.timeout(self.settings.request_deadline_seconds):
                with usage_context(UsageOwner(trace_id=run.trace_id, query_run_id=run.id)):
                    result = await self.runner.run(session, request, run, options=options)
        except (LLMError, TimeoutError) as error:
            await session.rollback()
            code = getattr(error, "failure_code", type(error).__name__)
            latency = round((time.monotonic() - started) * 1000)
            await self.finalize(
                session,
                run,
                latency=latency,
                failure_code=(
                    RunFailureCode.deadline_exceeded
                    if isinstance(error, TimeoutError)
                    else RunFailureCode.request_failed
                ),
            )
            return (
                EvaluationScore(
                    trial_id=trial.trial_id,
                    query_run_id=run.id,
                    latency_ms=latency,
                    failure_code=str(code)[:80],
                    included=False,
                ),
                None,
            )

        latency = round((time.monotonic() - started) * 1000)
        await self.finalize(session, run, latency=latency, result=result)
        citations = result.get("citations", [])
        # Citations carry the document UUID; the suite is keyed by arXiv ID.
        by_id = {str(value): key for key, value in self.documents.items()}
        for item in citations:
            item["arxiv_id"] = by_id.get(str(item.get("source_id")))

        insufficient = bool(result.get("insufficient", False))
        route = str(result.get("route", ""))
        # Recall and precision are ratios over target spans, and an unanswerable case has
        # none. Leaving them unset keeps a refusal out of means the report takes over
        # measured ratios; refusal correctness is its own metric rather than a bit smuggled
        # into three fields that already mean something else.
        metrics = score_citations(case, citations) if case.answerable else {}
        inputs, outputs = await token_totals(session, run.id)
        score = EvaluationScore(
            trial_id=trial.trial_id,
            query_run_id=run.id,
            route_correct=float(route == EXPECTED_ROUTE.value),
            refusal_correct=None if case.answerable else float(insufficient and not citations),
            document_recall=metrics.get("document_recall"),
            support_recall=metrics.get("support_recall"),
            citation_precision=metrics.get("citation_precision"),
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


def scored_trials(path: Path) -> set[uuid.UUID]:
    """Read the resume ledger, tolerating a final line torn by an interrupted append.

    Only the last line can be partial: everything before it was followed by a completed
    write. Refusing the whole file for one torn tail would strand a run that has already
    paid for hundreds of trials, and the torn trial simply re-runs.
    """
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    done: set[uuid.UUID] = set()
    for number, line in enumerate(lines, 1):
        try:
            done.add(uuid.UUID(cast(dict[str, Any], json.loads(line))["trial_id"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            if number != len(lines):
                raise SystemExit(
                    f"{path.name}:{number} is unreadable and is not the final line, so it was "
                    "not truncated by an interrupted write; repair it before resuming"
                ) from None
            # Dropped from the file as well as from the set: leaving it would let the next
            # append run onto the torn text and turn a recoverable tail into a permanent
            # mid-file corruption.
            path.write_text("".join(f"{kept}\n" for kept in lines[:-1]), encoding="utf-8")
            print(f"discarded a torn final line in {path.name}; that trial will re-run")
    return done


async def ensure_experiment(database: Database, manifest: FrozenEvaluationManifest) -> None:
    """Materialise the frozen plan as the experiment row the cost ceiling is read from.

    The plan is built on the host, which cannot reach the database, so the row is created
    here instead. Resuming re-enters this, so an existing row is left alone: rewriting the
    ceiling mid-run would let a resume raise a limit the plan was frozen with.
    """
    async with database.session() as session:
        if await session.get(Experiment, manifest.experiment_id) is not None:
            return
        session.add(
            Experiment(
                id=manifest.experiment_id,
                name=f"pageindex-v2-{manifest.created_at.strftime('%Y%m%dT%H%M%SZ')}",
                release_sha=manifest.release_sha,
                configuration_hash=manifest.configuration_sha256,
                corpus_hash=manifest.corpus_sha256,
                index_hash=manifest.index_sha256,
                dataset_hash=manifest.dataset_sha256,
                price_snapshot=manifest.price_snapshot,
                repetitions=manifest.repetitions,
                # The sweep varies this per trial; the row records the widest budget it can
                # reach, since that is what the ceiling has to survive.
                evidence_token_budget=max(manifest.evidence_budgets),
                seed=manifest.seed,
                maximum_cost_usd=Decimal(str(manifest.maximum_cost_usd)),
                frozen_at=manifest.created_at,
            )
        )
        await session.commit()
    print(f"experiment armed with a ${manifest.maximum_cost_usd:.2f} ceiling")


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
    done = scored_trials(scores_path)
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
        await ensure_experiment(database, manifest)
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
                AsyncPostgresSaver.from_conn_string(driver_free_url(settings.database_url))
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
            executor = Executor(runner, documents, settings, manifest.experiment_id)
            judge_path = args.run / "judge-queue.jsonl"
            for index, trial in enumerate(pending, 1):
                async with database.session() as session:
                    score, judge = await executor.execute(session, cases[trial.item_id], trial)
                # The judge entry is written first because scores.jsonl is the resume ledger:
                # a crash between the two writes leaves the trial pending and re-runs it,
                # which duplicates a judge line keyed by trial_id. The reverse order loses the
                # claim-recall input for a trial already marked done, which nothing recovers.
                if judge is not None:
                    with judge_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(judge, ensure_ascii=False) + "\n")
                with scores_path.open("a", encoding="utf-8") as handle:
                    handle.write(score.model_dump_json() + "\n")
                flag = "" if score.included else f" FAILED {score.failure_code}"
                print(
                    f"[{index}/{len(pending)}] {trial.experiment_kind} {score.latency_ms}ms{flag}"
                )
    finally:
        observability.flush()
        await sql_engine.dispose()
        await database.close()


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "evaluation"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=root / "run")
    parser.add_argument("--dataset", type=Path, default=root / "pageindex-v2-regression.jsonl")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="execute at most this many trials (0 runs every pending trial)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve documents and print the plan without calling the provider",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
