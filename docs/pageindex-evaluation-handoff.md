<!-- Content type: Reference -->
<!-- Content plan: Goal: let a fresh maintainer resume the PageIndex v2 evaluation without redoing its investigation. Audience: maintainers continuing the evaluation. Content: current state, decisions with rationale, diagnosed failure classes, operating recipes, measured costs, and remaining steps. Open questions: whether the capped partial baseline is sufficient, whether to authorize a new higher-cap experiment, and how to score the claim-recall queue. -->

# Resume the PageIndex v2 evaluation

State, decisions, and findings for the PageIndex v2 evaluation as of branch
`fix/pageindex-evaluation-dataset`, written against base commit
`a870e20c6b4e562594108c2d2d87667ff3969f7e`. Written so the work can continue
from the repository and this page alone. Reconfirm all anchors against the
current checkout before editing.

## Where it stands

| Item | State |
| --- | --- |
| Evaluation dataset | 120/120 approved: 40 by `gcharang`, 80 automated structural reviews explicitly marked “not a human read”; `make evaluation-check` clean |
| Corpus index | 104/104 documents indexed and ready |
| Index recipe | 98 documents on `+vr5`, 6 on `+vr6` — deliberate, see below |
| Trial executor | Bounded concurrency 4 verified live; maximum observed overlap 4 |
| Current run | Stopped safely at its frozen $25 ceiling after 227/1,440 trials |
| Partial report | 172 included, 55 excluded; claim recall not yet scored |
| Branch | No remote branch; 23 commits ahead of refreshed `origin/master` after this handoff update |

`make api-lint`, `make api-typecheck`, `make docs-check`, and `make compose-check`
pass. The provider-free API suite is 409 passed; the disposable PostgreSQL
integration stack is 10 passed.

## Do this next

The `$25` experiment is complete **as a capped partial run**, not as the full
1,440-trial baseline. Re-running `make evaluation-execute` may consume small
amounts of headroom released when the last in-flight calls finalized, but it
cannot make this a complete run and will add more cap-denied trials. Do not do
that by default.

1. Decide whether 172 included trials are enough for a partial regression
   diagnostic. Do not present them as a complete baseline.
2. If a complete 1,440-trial baseline is required, explicitly authorize a new,
   higher-cap experiment. The existing manifest and database ceiling are
   immutable; a new experiment repeats earlier trials rather than continuing
   their spend under a different ceiling. At the observed conservative exposure
   rate, a full run projects to roughly `$155`
   (`$24.290311 / 226 provider-active trials × 1,440 = $154.77`; moderate
   confidence because the capped prefix is not a guarantee of the remaining
   mix).
3. If `answer_claim_recall` is required, score the 35 entries in
   `apps/api/evaluation/run/judge-queue.jsonl`. No scorer/writer exists yet; add
   and review one before editing `scores.jsonl`.
4. Preserve the Git-ignored run directory and the experiment rows before
   preparing another manifest. `make evaluation-report` reproduces the current
   partial report in `apps/api/evaluation/run/summary.json`.

Concurrency is part of the frozen configuration hash. Execution defaults to the
manifest value and rejects a different `CONCURRENCY` override.

## Current capped parallel run

| Field | Value |
| --- | --- |
| Experiment | `09471839-de04-4f94-97aa-a34e31b86509` |
| Runtime release | `a870e20c6b4e562594108c2d2d87667ff3969f7e` |
| Runtime configuration | `52d6f1a262ed873d11dbce6d3d64dba30cfb6f807f6fc20a210b6969e833f8b6` |
| Reviewed corpus | `d7e2d075f9e20c4778269bf4de6b7b4d9158dc92e206eaeccf59a25954442b68` |
| Active index | `ae4a7407c9ec07b60ce580a2e547c4ebd3d40ecae7e4cb3f0bc0f76346a4ca5a` |
| Dataset | `821e973c6362b105e899586cac9c9c189c6deb98cf0de047a8a9b3f754a9f872` |
| Concurrency | 4; maximum observed overlapping model calls: 4 |
| Ceiling | `$25.000000` |
| Progress | 227 terminal memberships, frozen sequence 0–226; 0 nonterminal |
| Outcomes | 172 included; 44 `TimeoutError`; 10 `provider_rejected`; 1 `experiment_cost_cap` |
| Model accounting | 1,203 calls; `$1.241899` measured telemetry; `$23.051574` held for 54 `review_required` calls |
| Cap exposure | `$24.290311` (`$1.238737` finalized measured + `$23.051574` held) |
| Wall time | 67m54s, from first model-call start to last completion |
| Local artifacts | 227 scores, 35 judge items, and `summary.json` under `apps/api/evaluation/run` |
| State | Valid, capped partial run; not a complete 1,440-trial baseline |

The final database audit found no `reserved` reservation, no nonterminal
`ModelCall`, and no nonterminal `QueryRun`. The `$1.241899` telemetry total
includes `$0.003162` observed on calls whose final usage remained incomplete;
the cap correctly charges those calls at their full reservation instead. The
executor stopped scheduling as soon as the database denied a reservation, then
drained already-admitted work.

Coverage is uneven because this is a capped prefix:

| Workload | Scored | Included | Excluded |
| --- | ---: | ---: | ---: |
| `corpus_routing` | 45 | 28 | 17 |
| `forced_route_end_to_end` | 65 | 47 | 18 |
| `oracle_document_retrieval` | 51 | 51 | 0 |
| `route` | 66 | 46 | 20 |

The partial report contains retrieval and refusal metrics, but
`answer_claim_recall` has count zero because its 35 answerable end-to-end
results are only queued for judging. The exact 180-second deadline and
HTTP-200 structured-response rejection remain the only provider failure
classes; the parallel scheduler introduced no new failure class.

## Retired sequential run

| Field | Value |
| --- | --- |
| Experiment | `4b1098f7-81d8-4d7b-a6e5-7b7da6dcc60b` |
| Runtime release | `0dd3bbb94851f53cfec6b4dfe16915afb79a6a35` |
| Runtime configuration | `ea661c92dd99001b1faf152f50349142512c04e2c33ee614de394a33862a1656` |
| Reviewed corpus | `d7e2d075f9e20c4778269bf4de6b7b4d9158dc92e206eaeccf59a25954442b68` |
| Active index | `ae4a7407c9ec07b60ce580a2e547c4ebd3d40ecae7e4cb3f0bc0f76346a4ca5a` |
| Dataset | `821e973c6362b105e899586cac9c9c189c6deb98cf0de047a8a9b3f754a9f872` |
| Ceiling | `$25.000000` |
| Progress | 79 scores written; 80 durable memberships |
| Model accounting | 422 calls, `$0.766011` measured, `$7.683858` held for unknown final usage |
| State | Invalid: `replaced_by_parallel_execution` |

The run was stopped after the sequential wall time proved operationally
impractical. Most exclusions were full-catalog workloads that either reached
the exact 180-second production deadline or received an HTTP-200 structured
response the provider adapter could not validate. Failed or cancelled calls
without final token telemetry remain `review_required`; the cost trigger
correctly charges their worst-case reservation against the ceiling. The local
run directory, including all 79 scores, is preserved at
`/tmp/pageindex-evaluation-run-4b1098f7`.

The earlier experiment `b2c2c14a-d9d3-4b96-9894-cf48eee427cf` failed executor
preflight before any provider call. It is preserved in PostgreSQL as invalid
with reason `executor_preflight_failed`, zero memberships, zero model calls, and
zero spend. Its local manifest and empty score ledger were moved intact to
`/tmp/pageindex-evaluation-run-b2c2c14a`.

## Decisions and why

### Two configuration hashes is intended

The index carries 98 documents built under `+vr5` and 6 under `+vr6`. Do not
re-index the 98 to collapse this without a new reason.

Both `+vr6` changes alter behaviour only on paths those 98 documents provably
never took. Under `+vr5`, a front-loaded-ToC early return makes `meta_processor`
raise, and a subdivision failure raises; either one fails the document. All 98
succeeded, so none reached either path, at top level or in any sub-node. For
those documents the two revisions are behaviourally identical and the differing
hash reflects a version string, not a difference in how the trees were built.

Re-indexing would also cost more than time. Model sampling is non-deterministic,
so it would trade 98 known-good trees for 98 unknown ones, and fresh samples
demonstrably fail in new ways.

The test for whether a future `+vrN` warrants a re-index is not hash equality: it
is whether the change alters behaviour on paths that *successful* documents take.

### The corpus is rebuilt, not topped up

The 10 documents indexed before 2026-07-28 were on artifact schema v1
(`+vr2`/`+vr3`). A frozen index mixing two artifact schemas is not a valid
baseline, because retrieval differences would partly reflect pipeline version
rather than architecture. All 104 were rebuilt under one schema.

### The executor runs in the container, as the host user

The evaluation plan is written on the host by `run_evaluation.py`, but execution
needs the object store, whose port is not published outside the Compose network.
So `apps/api/evaluation` is bind-mounted and `make evaluation-execute` runs
`--user "$(id -u):$(id -g)"`, because the image user is uid 10001 and appends
must land as the host user. Rejected alternatives: running the executor on the
host (cannot reach MinIO) and streaming results out (breaks the append-only
resume ledger).

### Unanswerable cases do not report retrieval metrics

`document_recall`, `support_recall`, and `citation_precision` are ratios over
target spans, and an unanswerable case has none. They are left unset — which
`report_evaluation.py` already filters — and refusal correctness is its own
`refusal_correct` metric. Previously one refusal bit was written into all three,
and the report averages each metric over groups mixing strata, so every published
retrieval number blended 100 ratios with 20 refusals.

### Spend is bounded in the database, not by attention

`run_evaluation.py` minted an experiment UUID without creating the row, and no
`experiment_memberships` row carried a `query_run_id`, so the reservation path
found no experiment and `enforce_experiment_cost_cap` could never fire. A
1,440-trial run had no ceiling of any kind. `MAX_COST` is now required, recorded
in the frozen manifest, and materialised by the executor before the first trial;
resuming reuses the existing limit rather than raising it.

## What was wrong with ingestion

Seven distinct defects presented as two catch-all failure codes. Both codes
discard their evidence, so each needed capture rather than inference.

| Cause | Layer | Symptom |
| --- | --- | --- |
| Child had no `PYTHONPATH` | adapter | `ModuleNotFoundError: pageindex` |
| Raw control characters in model JSON | runtime | `pageindex_invalid_json` |
| `subtree_page_range` containment too strict | artifact | child outside its range |
| Reversed page range `4-3` | artifact | `ingestion_failed` |
| `tiktoken` refused `<\|endoftext\|>` in page text | artifact | `ingestion_failed` |
| `verify_toc` treated coverage as correctness | runtime | `pageindex_child_failed` |
| Failed subdivision propagated | runtime | `pageindex_child_failed` |

Two are worth understanding rather than just knowing about.

**`verify_toc` treated coverage as correctness.** It returned `0, []` without
checking a single title whenever the last placed entry fell in the document's
first half, assuming a table of contents spans its document. Academic papers
break that assumption: references and appendices routinely fill more than half the
pages. `2408.00724` is 22 pages with 11 correctly placed entries ending at page
10, acknowledgments on page 11 and bibliography from page 12 — a complete, correct
table of contents for the body, scored zero. `meta_processor` then found no
fallback mode left and discarded the document. Diagnostic tell: if the child log
shows `start verify_toc` with no following `check all items`, the table of
contents was never examined and the cause is structural, not model quality.

**A failed subdivision destroyed verified work.** `process_large_node_recursively`
let the nested `meta_processor` exception propagate out of the whole build, so
`2305.16264` — verified at 100% across 50 pages — was thrown away because one
20-page section would not split. It produces 67 nodes at depth 3 once allowed to
finish.

Retrying does not substitute for either fix: 0 of 4 requeued documents recovered
across repeated attempts, and one document failed six times. These are
reproducible, not flaky.

## What changes artifact identity

`configuration_hash` digests only `generator_commit` (the `<sha>+vrN` literal in
`config.py`) and `patch_sha256` (a digest of the patch file). `recipe_hash` wraps
that with the document and metadata digests. **The Python artifact layer is not
hashed**, and nothing re-derives either value on load — `artifact_ops.py` never
references them.

- Python-layer fixes (`pageindex_artifact.py`, `pageindex_adapter.py`, …) change
  no identity and force no re-index.
- Any byte change to `apps/api/patches/pageindex-runtime.patch` shifts
  `patch_sha256`, so newly built documents take a new hash. Existing artifacts
  still load; they are nominally different, not invalidated.

This distinction decided real work: two of the eight stuck documents were fixed in
Python for nothing, and the six needing the vendored runtime cost only their own
rebuild rather than the whole corpus.

## Costs, measured

- **Ingestion: $0.0016/page**, from four real `+vr5` builds (68 pages, $0.1094).
  An earlier $0.0063/page figure came from pre-fix data where overlapping page
  groups duplicated work — do not reuse it.
- The corpus is 2,350 pages across 104 documents, so a full re-index is roughly
  **$4–5 and two hours** at four worker replicas.
- **Current trials: $1.241899 measured telemetry over 1,203 model calls; the
  cap charges $1.238737 finalized measured plus $23.051574 held, or
  $24.290311 total exposure over 227 durable trials.** Failed provider calls
  often lack final token telemetry, so 54 `review_required` calls retain their
  worst-case reservation. Measured spend alone is not a safe projection.
- **Retired sequential trials: $0.766011 measured plus $7.683858 held over 80
  durable trials.** Those rows remain invalid with reason
  `replaced_by_parallel_execution` and do not count against the current
  experiment.

## Reuse map

Extend these existing surfaces; do not create a second planning, accounting, or
reporting path:

| Responsibility | Existing surface |
| --- | --- |
| Frozen schemas, hashes, trial IDs, and bootstrap intervals | `apps/api/src/vectorless_rag/evaluation_suite.py` |
| Clean-release plan creation | `apps/api/scripts/run_evaluation.py` |
| Resume ledger, bounded workers, durable terminalization | `apps/api/scripts/execute_evaluation.py` |
| Provider-call reservation and reconciliation | `apps/api/src/vectorless_rag/usage.py` |
| Database-authoritative cap trigger | `apps/api/alembic/versions/0005_measurement_experiments.py` |
| Partial/full aggregate report | `apps/api/scripts/report_evaluation.py` |
| Active-index and corpus digest verification | `apps/api/scripts/freeze_index.py` |
| Operator entry points | Root `Makefile`, targets prefixed `evaluation-` |

## Operating recipes

**Database.** The application schema is `agent` in database `rag`, owner
`rag_owner` — not the `POSTGRES_USER`/`POSTGRES_DB` the container advertises, and
there is no `vectorless` role.

```bash
docker compose exec -T postgres psql -U rag_owner -d rag -t -A -F'|' -c "…"
```

Postgres is published on host port 55432. MinIO's API port is not published,
which is why anything needing the object store runs in-container.

**Requeue documents.** `sync_corpus` with `force=false` creates no job at all for
an already-registered document, so a plain `make sync-corpus` will not retry a
failure. Post `{"arxiv_ids": [...], "mode": "rebuild_index"}` to
`/v1/admin/ingestion/sync` (100 identifiers maximum per request). Mint the key
with `vectorless-rag bootstrap-key --scopes admin:ingest` and capture it straight
into a shell variable; never echo it.

**Scale the worker.** `docker compose up -d --no-deps --scale worker=N worker` is
safe — `claim_ingestion_job` uses `FOR UPDATE SKIP LOCKED`. Four replicas took the
corpus from about eleven hours to two. Recreating replicas mid-run is also safe:
leases expire and reclaim jobs as `stale_attempt_reclaimed` without consuming
retry budget. Note that `docker compose exec worker` reaches only one replica, so
verify deployed code with `docker exec` per container name.

**Capture a failing child.** Both failure codes discard the traceback. Temporarily
replace `stdout=DEVNULL, stderr=DEVNULL` in `PageIndexAdapter._run_v2_child` with
a file sink at `workspace.parent / f"diag-{workspace.name}.log"`; `/workspaces` is
a shared named volume that outlives the per-run `TemporaryDirectory`. Patch
`_run_v2_child`, not `index()` — `index()` is the v1 path, so patching it yields
silence and one red adapter test. For exceptions after the child succeeds, log the
traceback from the final `else` in `ingestion.py`, where `logger` is not in scope.
Correlate a log to a document whose *latest* job is failed and whose log is
non-empty: an in-flight retry has an open, empty log and otherwise reads as a
crash.

**Regenerate the vendored patch.** Do not hand-edit the diff.

```bash
docker compose cp worker:/opt/pageindex /tmp/pageindex
# /tmp/pageindex is a git clone at the pinned revision with the patch applied
# but uncommitted, so `git diff` there reproduces the tracked patch exactly.
# Edit the source, then:
git -C /tmp/pageindex diff > apps/api/patches/pageindex-runtime.patch
make pageindex-patch-test PAGEINDEX_SOURCE=/tmp/pageindex
```

Verify with `git apply --check` against a pristine copy, and bump `+vrN` in both
`config.py` and `tests/test_config.py`.

## Traps

- Never pass `--config apps/api/pyproject.toml` to make ruff cover root
  `scripts/`. It makes ruff treat `vectorless_rag` as third-party and reorders
  first-party imports across about 35 files. Root `scripts/` carries its own
  `ruff.toml` for exactly this reason.
- `.env` sets `PILOT_DOCUMENT_LIMIT=104` for this corpus, so `make pilot-run`
  refuses until it is restored to 25. The interlock is working, not broken.
- `python` is rewritten to `uv run python` by a hook; in-container use
  `sh -c 'python …'`.

## Deliberately not done

- `corpus_routing` and `forced_route_end_to_end` produce identical
  `GraphRunOptions`, so 360 of 1,440 trials duplicate another 360 and differ only
  in what is scored from the result. Redundant rather than wrong; about $4.
- `covered_pages` overstates coverage on widened trees, because
  `_unmapped_ranges` works from root subtree spans and cannot see a gap inside a
  widened subtree. Inert: nothing reads that counter, and retrieval uses
  `subtree_page_range`.
- `unmapped_pages` is 0 for every document because each final section's range
  extends to the document end, so references and appendices sit inside the last
  node and can be retrieved as evidence for a final-section hit. Pre-existing and
  corpus-wide, not introduced by `+vr6`.
- `direct_page_ranges`, `widened_nodes`, and `widened_pages` have no consumers.
  They are built, digested, and revalidated on load, but never read.

## Verification

```bash
make api-lint && make api-typecheck && make docs-check && make compose-check
cd apps/api && uv run pytest          # expect 409 passed, 10 skipped
TEST_POSTGRES_PORT=55433 make test-integration-stack  # expect 10 passed
make pageindex-patch-test PAGEINDEX_SOURCE=/opt/pageindex
```

A final unbiased vector-versus-PageIndex verdict still needs a new sealed dataset
created after both implementations and their indexes freeze. This suite is a
PageIndex v2 regression baseline, and it is visible to future vector development.

## Appendix: current-state anchors

The snippets below—and any line numbers derived from them—are as of base
`a870e20c6b4e562594108c2d2d87667ff3969f7e`. Always read the symbol again before
editing. These anchors are the evidence for the edit targets and invariants
described above.

### Frozen identity includes concurrency and cost

`apps/api/src/vectorless_rag/evaluation_suite.py`,
`evaluation_configuration_sha256` and `FrozenEvaluationManifest`:

```python
def evaluation_configuration_sha256(
    settings: Settings,
    *,
    execution_concurrency: int,
) -> str:
    snapshot = {name: getattr(settings, name) for name in EVALUATION_RUNTIME_SETTING_NAMES}
    snapshot["execution_concurrency"] = execution_concurrency
```

```python
class FrozenEvaluationManifest(StrictEvaluationModel):
    schema_version: Literal[2]
    experiment_id: uuid.UUID
    release_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price_snapshot: dict[str, float]
    repetitions: int = Field(ge=1, le=3)
    evidence_budgets: tuple[Literal[6400, 12800, 25600], ...]
    execution_concurrency: int = Field(ge=1, le=8)
    seed: int
    maximum_cost_usd: Decimal = Field(
        gt=Decimal("0"),
        max_digits=14,
        decimal_places=6,
        allow_inf_nan=False,
    )
```

The current local `manifest.json` pins schema 2, experiment
`09471839-de04-4f94-97aa-a34e31b86509`, concurrency 4, cost `$25`, and release
`a870e20c6b4e562594108c2d2d87667ff3969f7e`.

### The coordinator is bounded and is the only ledger writer

`apps/api/scripts/execute_evaluation.py`, `execute_pending_trials`:

```python
for executor in executors[: len(pending)]:
    schedule(executor)

try:
    while inflight:
        done, _ = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
```

```python
while next_to_write in completed:
    trial, score, judge = completed.pop(next_to_write)
    append_trial_result(scores_path, judge_path, score, judge)
    next_to_write += 1

if not budget_exhausted:
    for executor in available:
        if next_to_schedule == len(pending):
            break
        schedule(executor)
```

Each `Executor` owns one checkpointer and gets one database session per task.
Completed results buffer by frozen position, and a cap-denied result prevents
new scheduling while admitted work drains.

### Cancellation terminalizes any durable running row

`apps/api/scripts/execute_evaluation.py`, `Executor.execute`:

```python
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
```

Tests cover cancellation during run creation, inside the graph, and during
successful finalization. Do not narrow this handler back to the provider call.

### The database serializes cap decisions

`apps/api/alembic/versions/0005_measurement_experiments.py`,
`agent.enforce_experiment_cost_cap`:

```sql
SELECT maximum_cost_usd INTO cap
FROM agent.experiments
WHERE id = NEW.experiment_id
FOR UPDATE;
```

```sql
IF committed + NEW.reserved_cost_usd > cap THEN
  RAISE EXCEPTION 'experiment cost cap exceeded';
END IF;
```

`test_concurrent_reservations_serialize_at_the_experiment_cap` in
`apps/api/tests/test_postgres_integration.py` holds the winning transaction open
across two connections and proves exactly one reservation fits a one-reservation
cap.

### Claim recall has a queue but no scorer

`apps/api/scripts/execute_evaluation.py`, `Executor.score_terminal_run`:

```python
if trial.experiment_kind == "forced_route_end_to_end" and case.answerable:
    judge = {
        "trial_id": str(trial.trial_id),
        "item_id": str(case.item_id),
        "claims": [claim.claim for claim in case.atomic_claims],
        "answer": run.answer or "",
    }
```

`report_evaluation.py` reads `answer_claim_recall` from `scores.jsonl`; no
current script consumes `judge-queue.jsonl` or writes that field. A future
scorer must join by `trial_id`, validate one result per queued trial, and update
the resume ledger atomically.

### A higher ceiling requires a new experiment

Current immutable state:

```text
experiment_id = 09471839-de04-4f94-97aa-a34e31b86509
maximum_cost_usd = 25.000000
```

Target transition, only after explicit authorization:

```bash
test ! -e /tmp/pageindex-evaluation-run-09471839
mv apps/api/evaluation/run /tmp/pageindex-evaluation-run-09471839
make evaluation-run \
  INDEX_HASH=ae4a7407c9ec07b60ce580a2e547c4ebd3d40ecae7e4cb3f0bc0f76346a4ca5a \
  MAX_COST=<new-authorized-ceiling> CONCURRENCY=4
```

That command must create a new experiment UUID and clean manifest. Do not edit
the existing JSON or `agent.experiments.maximum_cost_usd`: either mutation
breaks the frozen-protocol claim. Rejected alternatives are raising the current
row in place and copying old scores into a differently bounded experiment.
