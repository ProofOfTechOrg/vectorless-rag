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
import hashlib
import json
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings, get_settings
from vectorless_rag.db import Database, driver_free_url, make_engine
from vectorless_rag.evaluation_suite import (
    ClaimJudgeItem,
    EvaluationCase,
    EvaluationCorpusManifest,
    EvaluationScore,
    EvaluationTrial,
    FrozenEvaluationManifest,
    build_trials,
    evaluation_configuration_sha256,
    evaluation_ledger_lock,
    evaluation_price_snapshot,
    file_sha256,
    frozen_index_sha256,
    load_cases,
    load_corpus_manifest,
    validate_indexed_corpus,
)
from vectorless_rag.graph import GraphResult, GraphRunner, GraphRunOptions
from vectorless_rag.llm import MAX_PROVIDER_ATTEMPTS, DeepSeekLLM, LLMError
from vectorless_rag.models import (
    ApiKey,
    Document,
    DocumentStatus,
    Experiment,
    ExperimentIndexVersion,
    ExperimentMembership,
    IndexVersion,
    ModelCall,
    QueryRun,
    QueryRunStatus,
    RunFailureCode,
    RunRoute,
    query_run_status,
)
from vectorless_rag.observability import Observability
from vectorless_rag.provider import ProviderGate
from vectorless_rag.retrieval import CorpusRouter, PageIndexRetriever, VectorlessRetriever
from vectorless_rag.schemas import ChatRequest, MetadataConstraints
from vectorless_rag.sql_guard import PostgreSQLAdapter
from vectorless_rag.storage import S3ObjectStore
from vectorless_rag.usage import ExperimentBudgetExceeded, UsageOwner, usage_context

# Every stratum in this suite is answerable from document text, so the router is correct when
# it chooses documents. Unanswerable cases still route to documents; being unable to answer is
# a retrieval outcome, measured by insufficient rather than by the route.
EXPECTED_ROUTE = RunRoute.documents
IMAGE_RELEASE_PATH = Path("/app/.release")
EXECUTION_CANCELLED_FAILURE_CODE = "execution_cancelled"
MAX_TRIAL_LIMIT = 10_000


def options_for(
    kind: str,
    budget: int,
    oracle: tuple[uuid.UUID, ...],
    index_version_ids: tuple[tuple[uuid.UUID, uuid.UUID], ...] = (),
) -> GraphRunOptions:
    """Map an experiment kind onto the graph's frozen-evaluation overrides.

    route leaves the router free so its choice can be scored; the rest pin the route so the
    retrieval stages are measured without routing noise standing in front of them.
    """
    if kind == "route":
        return GraphRunOptions(
            evidence_token_budget=budget,
            index_version_ids=index_version_ids,
        )
    if kind == "oracle_document_retrieval":
        return GraphRunOptions(
            forced_route=EXPECTED_ROUTE,
            oracle_document_ids=oracle,
            evidence_token_budget=budget,
            index_version_ids=index_version_ids,
        )
    return GraphRunOptions(
        forced_route=EXPECTED_ROUTE,
        evidence_token_budget=budget,
        index_version_ids=index_version_ids,
    )


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
        api_key_id: uuid.UUID,
        index_version_ids: dict[uuid.UUID, uuid.UUID],
    ) -> None:
        self.runner = runner
        self.documents = documents
        self.settings = settings
        self.experiment_id = experiment_id
        self.api_key_id = api_key_id
        self.index_version_ids = tuple(
            sorted(index_version_ids.items(), key=lambda item: str(item[0]))
        )

    async def finalize(
        self,
        session: AsyncSession,
        run: QueryRun,
        *,
        latency: int,
        result: GraphResult | None = None,
        failure_code: RunFailureCode | None = None,
        evaluation_failure_code: str | None = None,
    ) -> None:
        """Terminalize the QueryRun the way the HTTP surface does.

        query_run_status reads answer and failure_code; a row carrying neither stays
        `running` forever and makes query-cost-report treat the whole window as incomplete.
        failure_code carries the coarse category the serving report groups by; error carries
        the bounded evaluation code so a resume can reconstruct the exact file score.
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
            coarse_failure = failure_code or RunFailureCode.request_failed
            run.failure_code = coarse_failure
            run.error = evaluation_failure_code or coarse_failure.value
            run.insufficient = True
        await session.commit()

    async def execute(
        self,
        session: AsyncSession,
        case: EvaluationCase,
        trial: EvaluationTrial,
    ) -> tuple[EvaluationScore, ClaimJudgeItem | None]:
        run_id = trial_run_id(self.experiment_id, trial.trial_id)
        lifecycle_started = time.monotonic()
        try:
            return await self._execute(session, case, trial)
        except asyncio.CancelledError:
            latency = round((time.monotonic() - lifecycle_started) * 1000)

            async def terminalize_cancelled_run() -> None:
                await session.rollback()
                durable_run = await session.get(QueryRun, run_id, populate_existing=True)
                if durable_run is None or query_run_status(durable_run) != QueryRunStatus.running:
                    return
                await self.finalize(
                    session,
                    durable_run,
                    latency=latency,
                    failure_code=RunFailureCode.request_failed,
                    evaluation_failure_code=EXECUTION_CANCELLED_FAILURE_CODE,
                )

            cleanup = asyncio.create_task(
                terminalize_cancelled_run(),
                name=f"terminalize-cancelled-evaluation-{trial.trial_id}",
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup
            raise

    async def _execute(
        self,
        session: AsyncSession,
        case: EvaluationCase,
        trial: EvaluationTrial,
    ) -> tuple[EvaluationScore, ClaimJudgeItem | None]:
        oracle = tuple(
            self.documents[target.arxiv_id]
            for target in case.target_documents
            if target.arxiv_id in self.documents
        )
        run, created = await create_trial_run(
            session,
            experiment_id=self.experiment_id,
            api_key_id=self.api_key_id,
            question=case.prompt,
            trial=trial,
        )
        if not created:
            if query_run_status(run) == QueryRunStatus.running:
                raise SystemExit(
                    f"trial {trial.trial_id} has a nonterminal persisted query run; "
                    "inspect it before resuming"
                )
            return await self.score_terminal_run(session, case, trial, run)

        # Built outside the try so a malformed case raises instead of being recorded as an
        # excluded trial: a bad dataset is a defect, not a provider failure.
        request = ChatRequest(
            message=case.prompt,
            constraints=MetadataConstraints.model_validate(case.constraints),
        )
        options = options_for(
            trial.experiment_kind,
            trial.evidence_token_budget,
            oracle,
            self.index_version_ids,
        )

        started = time.monotonic()
        try:
            # The same deadline the HTTP surface enforces. Without it this scores as a clean
            # success exactly the long multi-round runs that production returns as 504, which
            # would bias the report toward the hardest cases the suite exists to measure.
            async with asyncio.timeout(self.settings.request_deadline_seconds):
                with usage_context(
                    UsageOwner(
                        trace_id=run.trace_id,
                        query_run_id=run.id,
                        require_cost_reservation=True,
                    )
                ):
                    result = await self.runner.run(session, request, run, options=options)
        except (ExperimentBudgetExceeded, LLMError, TimeoutError) as error:
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
                evaluation_failure_code=str(code)[:80],
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
        return await self.score_terminal_run(session, case, trial, run)

    async def score_terminal_run(
        self,
        session: AsyncSession,
        case: EvaluationCase,
        trial: EvaluationTrial,
        run: QueryRun,
    ) -> tuple[EvaluationScore, ClaimJudgeItem | None]:
        if query_run_status(run) == QueryRunStatus.failed:
            return (
                EvaluationScore(
                    trial_id=trial.trial_id,
                    query_run_id=run.id,
                    latency_ms=run.latency_ms,
                    failure_code=run.error
                    or (
                        run.failure_code.value
                        if run.failure_code is not None
                        else RunFailureCode.request_failed.value
                    ),
                    included=False,
                ),
                None,
            )

        citations = [dict(item) for item in run.citations]
        # Citations carry the document UUID; the suite is keyed by arXiv ID.
        by_id = {str(value): key for key, value in self.documents.items()}
        for item in citations:
            item["arxiv_id"] = by_id.get(str(item.get("source_id")))

        insufficient = run.insufficient
        route = run.route.value if run.route is not None else ""
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
            latency_ms=run.latency_ms,
            input_tokens=inputs,
            output_tokens=outputs,
        )
        judge = None
        if trial.experiment_kind == "forced_route_end_to_end" and case.answerable:
            judge = ClaimJudgeItem(
                trial_id=trial.trial_id,
                item_id=case.item_id,
                claims=[claim.claim for claim in case.atomic_claims],
                answer=run.answer or "",
            )
        return score, judge


def append_judge_item(path: Path, judge: ClaimJudgeItem) -> None:
    items: list[ClaimJudgeItem] = []
    if path.exists():
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            1,
        ):
            if not line.strip():
                continue
            try:
                item = ClaimJudgeItem.model_validate_json(line)
            except ValueError as error:
                raise SystemExit(f"{path.name}:{line_number}: {error}") from error
            if item.trial_id == judge.trial_id:
                if item != judge:
                    raise SystemExit(
                        f"{path.name} has conflicting content for trial {judge.trial_id}"
                    )
                return
            items.append(item)
    items.append(judge)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            for item in items:
                handle.write(item.model_dump_json() + "\n")
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def append_trial_result(
    scores_path: Path,
    judge_path: Path,
    score: EvaluationScore,
    judge: ClaimJudgeItem | None,
) -> None:
    # The judge entry is written first because scores.jsonl is the resume ledger: a crash
    # between the two writes leaves the trial pending and reconstructs it from the terminal
    # QueryRun. The reverse order loses claim-recall input for a trial already marked done.
    with evaluation_ledger_lock(scores_path.parent):
        if judge is not None:
            append_judge_item(judge_path, judge)
        with scores_path.open("a", encoding="utf-8") as handle:
            handle.write(score.model_dump_json() + "\n")


async def execute_pending_trials(
    database: Database,
    executors: Sequence[Executor],
    pending: Sequence[EvaluationTrial],
    cases: Mapping[uuid.UUID, EvaluationCase],
    scores_path: Path,
    judge_path: Path,
) -> bool:
    """Run a bounded trial window while one writer preserves plan order.

    Each Executor owns a distinct LangGraph checkpointer connection and can have at most one
    in-flight trial. Results may finish out of order, but only this coordinator appends the
    ledgers, in frozen sequence order. If the database rejects a new reservation at the cost
    ceiling, no more trials are scheduled and already-reserved work is allowed to finish.
    """

    async def execute_one(
        executor: Executor,
        trial: EvaluationTrial,
    ) -> tuple[EvaluationScore, ClaimJudgeItem | None]:
        async with database.session() as session:
            return await executor.execute(session, cases[trial.item_id], trial)

    inflight: dict[
        asyncio.Task[tuple[EvaluationScore, ClaimJudgeItem | None]],
        tuple[int, Executor],
    ] = {}
    completed: dict[
        int,
        tuple[EvaluationTrial, EvaluationScore, ClaimJudgeItem | None],
    ] = {}
    next_to_schedule = 0
    next_to_write = 0
    budget_exhausted = False

    def schedule(executor: Executor) -> None:
        nonlocal next_to_schedule
        position = next_to_schedule
        trial = pending[position]
        task = asyncio.create_task(
            execute_one(executor, trial),
            name=f"evaluation-trial-{trial.trial_id}",
        )
        inflight[task] = (position, executor)
        next_to_schedule += 1

    for executor in executors[: len(pending)]:
        schedule(executor)

    try:
        while inflight:
            done, _ = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
            available: list[Executor] = []
            for task in sorted(done, key=lambda item: inflight[item][0]):
                position, executor = inflight.pop(task)
                score, judge = task.result()
                completed[position] = (pending[position], score, judge)
                available.append(executor)
                if score.failure_code == ExperimentBudgetExceeded.failure_code:
                    budget_exhausted = True

            while next_to_write in completed:
                trial, score, judge = completed.pop(next_to_write)
                append_trial_result(scores_path, judge_path, score, judge)
                flag = "" if score.included else f" FAILED {score.failure_code}"
                print(
                    f"[{next_to_write + 1}/{len(pending)}] {trial.experiment_kind} "
                    f"{score.latency_ms}ms{flag}",
                    flush=True,
                )
                next_to_write += 1

            if not budget_exhausted:
                for executor in available:
                    if next_to_schedule == len(pending):
                        break
                    schedule(executor)
    except BaseException:
        for task in inflight:
            task.cancel()
        await asyncio.gather(*inflight, return_exceptions=True)
        raise

    return budget_exhausted


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


def validate_frozen_inputs(
    settings: Settings,
    manifest: FrozenEvaluationManifest,
    trials: list[EvaluationTrial],
    cases: list[EvaluationCase],
    *,
    dataset_sha256: str,
    corpus: EvaluationCorpusManifest,
    image_release: str,
    execution_concurrency: int,
) -> None:
    if settings.release != manifest.release_sha:
        raise SystemExit("runtime RELEASE does not match the frozen manifest")
    if image_release != manifest.release_sha:
        raise SystemExit("built image release does not match the frozen manifest")
    if dataset_sha256 != manifest.dataset_sha256:
        raise SystemExit("evaluation dataset does not match the frozen manifest")
    if corpus.corpus_snapshot_sha256 != manifest.corpus_sha256:
        raise SystemExit("evaluation corpus does not match the frozen manifest")
    if execution_concurrency != manifest.execution_concurrency:
        raise SystemExit("execution concurrency does not match the frozen manifest")
    if (
        evaluation_configuration_sha256(
            settings,
            execution_concurrency=execution_concurrency,
        )
        != manifest.configuration_sha256
    ):
        raise SystemExit("evaluation runtime configuration does not match the frozen manifest")
    try:
        current_prices = evaluation_price_snapshot(
            settings, maximum_provider_attempts=MAX_PROVIDER_ATTEMPTS
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if current_prices != manifest.price_snapshot:
        raise SystemExit("provider prices or reservation bounds changed after the run was frozen")
    expected_trials = build_trials(
        cases,
        seed=manifest.seed,
        repetitions=manifest.repetitions,
    )
    if trials != expected_trials:
        raise SystemExit("evaluation trials do not match the frozen dataset and seed")


def bind_frozen_index(
    rows: Sequence[tuple[str | None, uuid.UUID, uuid.UUID, str, uuid.UUID | None]],
    expected_sha256: str,
) -> tuple[dict[str, uuid.UUID], dict[uuid.UUID, uuid.UUID]]:
    missing_metadata = [str(row[2]) for row in rows if row[4] is None]
    if missing_metadata:
        raise SystemExit(
            f"{len(missing_metadata)} frozen index version(s) have no metadata snapshot: "
            f"{missing_metadata[:5]}"
        )
    try:
        current_sha256 = frozen_index_sha256([(row[0], row[3]) for row in rows])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if current_sha256 != expected_sha256:
        raise SystemExit("active index does not match the frozen manifest")
    return (
        {str(row[0]): row[1] for row in rows},
        {row[1]: row[2] for row in rows},
    )


async def create_trial_run(
    session: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    api_key_id: uuid.UUID,
    question: str,
    trial: EvaluationTrial,
) -> tuple[QueryRun, bool]:
    run_id = trial_run_id(experiment_id, trial.trial_id)
    thread_id = f"eval-{experiment_id}-{trial.trial_id}"
    existing = await session.get(QueryRun, run_id)
    if existing is not None:
        membership = await session.scalar(
            select(ExperimentMembership).where(ExperimentMembership.query_run_id == run_id)
        )
        if (
            membership is None
            or membership.experiment_id != experiment_id
            or membership.trial_key != str(trial.trial_id)
            or membership.repetition != trial.repetition
            or membership.sequence_number != trial.sequence_number
            or existing.api_key_id != api_key_id
            or existing.thread_id != thread_id
            or existing.question != question
        ):
            raise SystemExit("persisted evaluation trial identity does not match the frozen plan")
        return existing, False

    run = QueryRun(
        id=run_id,
        trace_id=uuid.uuid4().hex,
        thread_id=thread_id,
        api_key_id=api_key_id,
        question=question,
    )
    session.add(run)
    await session.flush([run])
    # The cost ceiling resolves each model call through this membership. Persist the run
    # first so the membership cannot race its foreign key during SQLAlchemy's unit of work.
    session.add(
        ExperimentMembership(
            experiment_id=experiment_id,
            query_run_id=run.id,
            trial_key=str(trial.trial_id),
            repetition=trial.repetition,
            sequence_number=trial.sequence_number,
            included=True,
        )
    )
    await session.commit()
    return run, True


def trial_run_id(experiment_id: uuid.UUID, trial_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(experiment_id, str(trial_id))


def trial_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 0 <= limit <= MAX_TRIAL_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be between 0 and {MAX_TRIAL_LIMIT}")
    return limit


async def ensure_experiment(
    session: AsyncSession,
    manifest: FrozenEvaluationManifest,
    index_version_ids: frozenset[uuid.UUID],
) -> uuid.UUID:
    """Materialise the frozen plan as the experiment row the cost ceiling is read from.

    The plan is built on the host, which cannot reach the database, so the row is created
    here instead. Resuming re-enters this, so an existing row is left alone: rewriting the
    ceiling mid-run would let a resume raise a limit the plan was frozen with.
    """
    api_key_id = uuid.uuid5(manifest.experiment_id, "disabled-evaluation-principal")
    prefix = f"eval{api_key_id.hex[:12]}"
    key_hash = hashlib.sha256(f"disabled-evaluation-principal:{api_key_id}".encode()).hexdigest()
    existing_key = await session.get(ApiKey, api_key_id)
    if existing_key is None:
        session.add(
            ApiKey(
                id=api_key_id,
                name=f"evaluation:{manifest.experiment_id}",
                prefix=prefix,
                key_hash=key_hash,
                scopes=[],
                disabled=True,
            )
        )
    elif (
        existing_key.prefix != prefix
        or existing_key.key_hash != key_hash
        or existing_key.scopes
        or not existing_key.disabled
    ):
        raise SystemExit("existing evaluation principal does not match the frozen experiment")

    existing_experiment = await session.get(Experiment, manifest.experiment_id)
    if existing_experiment is None:
        existing_experiment = Experiment(
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
            maximum_cost_usd=manifest.maximum_cost_usd,
            frozen_at=manifest.created_at,
        )
        session.add(existing_experiment)
        await session.flush([existing_experiment])
        session.add_all(
            ExperimentIndexVersion(
                experiment_id=manifest.experiment_id,
                index_version_id=index_version_id,
            )
            for index_version_id in index_version_ids
        )
    elif (
        not existing_experiment.valid
        or existing_experiment.release_sha != manifest.release_sha
        or existing_experiment.configuration_hash != manifest.configuration_sha256
        or existing_experiment.corpus_hash != manifest.corpus_sha256
        or existing_experiment.index_hash != manifest.index_sha256
        or existing_experiment.dataset_hash != manifest.dataset_sha256
        or existing_experiment.price_snapshot != manifest.price_snapshot
        or existing_experiment.repetitions != manifest.repetitions
        or existing_experiment.evidence_token_budget != max(manifest.evidence_budgets)
        or existing_experiment.seed != manifest.seed
        or existing_experiment.maximum_cost_usd != manifest.maximum_cost_usd
        or existing_experiment.frozen_at != manifest.created_at
    ):
        raise SystemExit("existing experiment does not match the frozen manifest")
    else:
        persisted_index_versions = frozenset(
            await session.scalars(
                select(ExperimentIndexVersion.index_version_id).where(
                    ExperimentIndexVersion.experiment_id == manifest.experiment_id
                )
            )
        )
        if persisted_index_versions != index_version_ids:
            raise SystemExit("existing experiment does not use the frozen index versions")
    return api_key_id


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
    case_list = load_cases(args.dataset)
    cases = {case.item_id: case for case in case_list}
    corpus = load_corpus_manifest(args.corpus)
    execution_concurrency = args.concurrency or manifest.execution_concurrency
    try:
        image_release = (
            await asyncio.to_thread(IMAGE_RELEASE_PATH.read_text, encoding="utf-8")
        ).strip()
    except OSError as error:
        raise SystemExit(f"cannot read the built image release: {error}") from error
    validate_frozen_inputs(
        settings,
        manifest,
        trials,
        case_list,
        dataset_sha256=file_sha256(args.dataset),
        corpus=corpus,
        image_release=image_release,
        execution_concurrency=execution_concurrency,
    )
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
                "concurrency": execution_concurrency,
            },
            indent=2,
        )
    )
    if not pending:
        return

    database = Database(settings)
    sql_engine = make_engine(
        settings.sql_database_url,
        pool_size=max(5, execution_concurrency),
    )
    store = S3ObjectStore(settings)
    observability = Observability(settings)
    provider_gate = ProviderGate(settings)
    llm = DeepSeekLLM(settings, observability, provider_gate=provider_gate)
    try:
        async with database.session() as session:
            rows = (
                await session.execute(
                    select(
                        Document.arxiv_id,
                        Document.id,
                        IndexVersion.id,
                        IndexVersion.artifact_sha256,
                        IndexVersion.metadata_version_id,
                        Document.sha256,
                    )
                    .join(IndexVersion, Document.active_index_version_id == IndexVersion.id)
                    .where(Document.status == DocumentStatus.ready)
                    .order_by(Document.arxiv_id)
                )
            ).all()
        try:
            validate_indexed_corpus([(row[0], str(row[5])) for row in rows], corpus)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        documents, index_version_ids = bind_frozen_index(
            [(row[0], row[1], row[2], str(row[3]), row[4]) for row in rows],
            manifest.index_sha256,
        )
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
        async with database.session() as session:
            api_key_id = await ensure_experiment(
                session,
                manifest,
                frozenset(index_version_ids.values()),
            )
        print(f"experiment armed with a ${manifest.maximum_cost_usd:.2f} ceiling")
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
            executors: list[Executor] = []
            for worker_number in range(execution_concurrency):
                checkpointer = await stack.enter_async_context(
                    AsyncPostgresSaver.from_conn_string(driver_free_url(settings.database_url))
                )
                if worker_number == 0:
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
                executors.append(
                    Executor(
                        runner,
                        documents,
                        settings,
                        manifest.experiment_id,
                        api_key_id,
                        index_version_ids,
                    )
                )
            judge_path = args.run / "judge-queue.jsonl"
            if await execute_pending_trials(
                database,
                executors,
                pending,
                cases,
                scores_path,
                judge_path,
            ):
                raise SystemExit(ExperimentBudgetExceeded.diagnostic)
    finally:
        observability.flush()
        await sql_engine.dispose()
        await database.close()


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "evaluation"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=root / "run")
    parser.add_argument("--dataset", type=Path, default=root / "pageindex-v2-regression.jsonl")
    parser.add_argument("--corpus", type=Path, default=root / "pageindex-v2-corpus.json")
    parser.add_argument(
        "--limit",
        type=trial_limit,
        default=0,
        metavar=f"0..{MAX_TRIAL_LIMIT}",
        help="execute at most this many trials (0 runs every pending trial)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve documents and print the plan without calling the provider",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=range(1, 9),
        metavar="1..8",
        help="must match the frozen plan; defaults to the manifest value",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
