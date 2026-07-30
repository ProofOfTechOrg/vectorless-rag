<!-- Content type: Reference -->
<!-- Content plan: Goal: let a fresh maintainer resume the PageIndex v2 evaluation without redoing its investigation. Audience: maintainers continuing the evaluation. Content: current state, decisions with rationale, diagnosed failure classes, implemented corrections, operating recipes, measured costs, and remaining steps. Open questions: whether to retain the capped partial run only as historical evidence and who will score the claim-recall queue. -->

# Resume the PageIndex v2 evaluation

This reference records the PageIndex v2 evaluation state, the defects found in its capped run, and the corrected procedure. It covers branch `fix/pageindex-evaluation-dataset` with working-tree fixes based on commit `74016bbdf5d0ee0b77d735bc5c7a297b4341b6c2`. Reconfirm every anchor against the checkout before editing because the fixes described below are not committed at this baseline.

## Where it stands

| Item | State |
| --- | --- |
| Evaluation dataset | 120/120 approved: 40 by `gcharang`, 80 automated structural reviews explicitly marked “not a human read”; `make evaluation-check` clean |
| Corpus index | 104/104 documents indexed and ready |
| Index recipe | 98 documents on `+vr5`, 6 on `+vr6` — deliberate, see below |
| Trial executor | Historical bounded concurrency 4 verified live; compact parallel catalog routing now has provider-free coverage |
| Current run | Stopped safely at its frozen $25 ceiling after 227/1,440 trials |
| Partial report | 172 included, 55 excluded; claim recall not yet scored |
| Corrected trial plan | 1,080 trials; removes 360 duplicate graph executions |
| Corrected cost control | Request-bounded reservations and terminal unknown-attempt reconciliation at schema head `0006_bound_cost_reservations` |
| Branch | No remote branch; 23 commits ahead of refreshed `origin/master`, plus the uncommitted fixes described here |

`make api-lint`, `make api-typecheck`, `make docs-check`, and `make compose-check`
pass. The provider-free API suite is 430 passed, 13 skipped; the disposable
PostgreSQL integration stack is 13 passed.

## Do this next

The `$25` experiment remains a valid capped partial run, not a complete baseline. Do not resume it: its 1,440-trial plan, runtime release, and accounting protocol are frozen.

No budget increase is justified by this run. The retired `$155` projection extrapolated conservative cap exposure, not provider spend. That exposure included `$23.051574` held by 54 uncertain calls, each reserved as three attempts at the frozen 1 million input-token and 8,192 output-token limits. It also included a trial plan with 360 duplicate graph executions.

Continue in this order:

1. Review and commit the working-tree fixes, rebuild the application, and migrate PostgreSQL to `0006_bound_cost_reservations`.
2. Preserve `apps/api/evaluation/run` as the historical run for experiment `09471839-de04-4f94-97aa-a34e31b86509`.
3. Create a new 1,080-trial experiment with the same `$25` ceiling. Do not raise the ceiling unless the corrected run reaches it and the database audit shows legitimate measured or request-bounded exposure.
4. Run `make evaluation-execute DRY_RUN=1`, then a small `LIMIT=12` live canary before continuing the experiment. The dry run validates identity and index bindings but does not estimate cost.
5. Score answer claims with `make evaluation-judge REVIEWER=your_reviewer_name_here`, then rebuild `summary.json` with `make evaluation-report`.

Concurrency is part of the frozen configuration hash. Execution defaults to the
manifest value and rejects a different `CONCURRENCY` override.

## Defects corrected after the capped run

The capped run and the independent clean-code, architecture, and QA passes
exposed the defects below. The working tree fixes each root cause and retains
historical read compatibility.

| Defect | Evidence from the capped run | Correction |
| --- | --- | --- |
| Full-catalog routing was serial and failed batches leaked sibling tasks | A 104-document catalog generated one model call per document; 44 trials reached the exact 180s request deadline after 4 to 13 completed calls | Pack up to 32 compact catalog entries per map call, run map and reduction batches concurrently, and cancel plus join every sibling when any batch fails |
| Malformed HTTP-200 responses did not retry on both provider call paths | Ten document-selection calls returned HTTP 200 without valid structured output and were classified `provider_rejected` | Classify `openai.APIResponseValidationError` as retryable `upstream_invalid_response`, and use that shared classification in both the application LLM and PageIndex runtime retry loops |
| An older successful call could erase a newer provider-blocking failure | Completion success unconditionally persisted `available`, even if another in-flight call or worker had since observed an authentication, permission, or credit failure | Completion success no longer changes provider availability; only a fresh status or balance observation can clear a hard failure |
| Reservations used frozen maximum bounds | The old ledger reserved 1 million input tokens plus 8,192 output tokens for all three attempts, independent of request size | Persist immutable per-call input, output, and attempt bounds; derive the input-token bound from serialized UTF-8 bytes plus a 4,096-byte request-wrapper allowance |
| An experiment-attached call could proceed without a reservation | The ledger treated “no experiment” and “attached experiment rejected by validity or bounds” as the same empty SQL result when reservation enforcement was optional | Return a rejection sentinel for an attached experiment that cannot reserve, and stop the call before inserting model-call telemetry regardless of the caller’s optional-reservation flag |
| Terminal uncertain calls retained unused attempts | One missing usage record kept the entire three-attempt reservation | On terminal reconciliation, retain measured usage plus one per-request bound for each attempt whose usage is unknown |
| Reservation-to-call relationships were application-only invariants | The schema allowed a call to reuse an attempt number, exceed its reserved attempt limit, use another logical call ID, attach to an owner outside the reservation’s experiment, or mutate reservation/owner identity after validation | Audit historical rows during migration, add a unique reserved-attempt constraint, validate logical ID, attempt bound, and experiment ownership in a database trigger, and make reservation identity/bounds plus attempted-job experiment bindings immutable |
| Two experiment kinds executed the same graph | `corpus_routing` and `forced_route_end_to_end` used identical `GraphRunOptions`; only their score extraction differed | Stop generating `corpus_routing` trials, reducing new plans from 1,440 to 1,080; keep the literal only for historical manifests |
| Claim recall had no writer | The current run has 35 durable queue entries and no claim scores | Add a strict, idempotent judgment ledger and atomic score updater in `judge_evaluation.py` |
| Claim judgments could target a changed answer, and scoring could overwrite a newly appended score | Judgment identity covered the trial, item, and claims but not the answer; atomic replacement serialized only within the scoring process | Bind each judgment to the queued answer’s SHA-256 and use one filesystem lock for executor appends, judgment writes, and score rewrites |
| A crash could duplicate or tear a claim queue entry | The executor wrote the judge queue before the score resume ledger, so recovery could append the same queue item again | Validate queue items and replace the queue atomically; treat an identical trial item as already written and reject conflicting content |
| Artifact coverage and reuse identity omitted Python-layer behavior | Internal direct-range gaps appeared covered, and Python artifact-builder changes altered neither the configuration hash nor the reuse key | Recurse over direct page ranges and include `ARTIFACT_BUILDER_VERSION` in both the configuration hash and artifact reuse key |

No live provider call or re-index has run against these corrections. The full API suite, migration render, and disposable PostgreSQL integration suite pass.

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
results await judgment. Run `make evaluation-judge-status` to check progress.
The exact 180s deadline and HTTP-200 structured-response rejection were the
only provider failure classes; the parallel scheduler introduced no new
failure class.

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

`configuration_hash` now digests `ARTIFACT_BUILDER_VERSION`, `generator_commit`, `patch_sha256`, the model, prompt profile, options profile, parser, and parser version. `recipe_hash` wraps that configuration with the document and metadata digests.

- Bump `ARTIFACT_BUILDER_VERSION` when Python artifact construction or normalization changes output semantics
- Bump the `+vrN` suffix in `generator_commit` when vendored PageIndex runtime behavior changes
- Expect any byte change to `apps/api/patches/pageindex-runtime.patch` to change `patch_sha256`

Existing artifacts still load because validation checks their stored digests.
The active frozen index therefore remains usable for this evaluation without a
re-index. A future `ensure_current` build intentionally does not reuse an older
artifact when its builder version differs, because that artifact was produced
under different Python construction semantics.

## Costs, measured

- **Ingestion: $0.0016/page**, from four real `+vr5` builds (68 pages, $0.1094).
  An earlier $0.0063/page figure came from pre-fix data where overlapping page
  groups duplicated work — do not reuse it.
- The corpus is 2,350 pages across 104 documents, so a full re-index is roughly
  **$4–5 and two hours** at four worker replicas.
- **Current trials: $1.241899 measured telemetry over 1,203 model calls; the
  old cap protocol charges $1.238737 finalized measured plus $23.051574 held,
  or $24.290311 total exposure over 227 durable trials.** This is historical
  evidence, not a forecast for the corrected protocol. Fifty-four
  `review_required` calls retain frozen maximum-bound reservations.
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
| Request-bound reservation schema and call invariants | `apps/api/alembic/versions/0006_bound_cost_reservations.py` |
| Partial/full aggregate report | `apps/api/scripts/report_evaluation.py` |
| Claim judgment and atomic score update | `apps/api/scripts/judge_evaluation.py` |
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

- `unmapped_pages` is 0 for every document because each final section's range
  extends to the document end, so references and appendices sit inside the last
  node and can be retrieved as evidence for a final-section hit. The builder
  cannot infer a semantically correct section boundary from the current model
  output, so changing this would invent structure.
- `widened_nodes` and `widened_pages` have no consumers. They are built,
  digested, and revalidated on load, but never read. `direct_page_ranges` now
  drives recursive unmapped-page accounting.
- Historical `corpus_routing` trials remain valid to parse and report. Only new
  plan generation excludes that duplicate experiment kind.
- The current 104-document index was not rebuilt. The coverage fix changes
  artifact metrics and identity for future builds, not retrieval from stored
  artifacts.

## Verification

```bash
make api-lint && make api-typecheck && make docs-check && make compose-check
make api-test                         # expect 430 passed, 13 skipped
TEST_POSTGRES_PORT=55433 make test-integration-stack  # expect 13 passed
make pageindex-patch-test PAGEINDEX_SOURCE=/opt/pageindex
```

A final unbiased vector-versus-PageIndex verdict still needs a new sealed dataset
created after both implementations and their indexes freeze. This suite is a
PageIndex v2 regression baseline, and it is visible to future vector development.

## Appendix: current-state anchors

The snippets below, and any line numbers derived from them, are from the working tree based on `74016bbdf5d0ee0b77d735bc5c7a297b4341b6c2`. Always read the symbol again before editing. These anchors record the corrected edit targets and invariants.

### New plans exclude the duplicate graph execution

`apps/api/src/vectorless_rag/evaluation_suite.py`, `EXPERIMENT_KINDS`:

```python
EXPERIMENT_KINDS = (
    "route",
    "oracle_document_retrieval",
    "forced_route_end_to_end",
)
```

`EvaluationTrial.experiment_kind` still accepts `corpus_routing`, so the current 1,440-trial manifest remains readable. `build_trials` iterates only `EXPERIMENT_KINDS`, so a one-repetition plan now contains 1,080 trials.

### Catalog routing uses compact concurrent batches

`apps/api/src/vectorless_rag/retrieval.py`, `CorpusRouter._batches` and `select_ranked`:

```python
maximum_batch_size = self.settings.document_limit * 4
```

```python
mapped = [
    item
    for batch_result in await _gather_or_cancel(
        [
            self._select_batch(
                question,
                batch,
                phase="map",
                selection_limit=self.settings.document_limit,
            )
            for batch in self._batches(candidates)
        ]
    )
    for item in batch_result
]
```

The regression test binds the current 104-document catalog to batch sizes `32,
32, 32, 8` and blocks each map task until all four have started. Reduction
batches use the same concurrent pattern. `_gather_or_cancel` cancels and awaits
every sibling before re-raising a batch failure; a separate regression proves
no sibling is left running.

### HTTP-200 validation failures retry

`apps/api/src/vectorless_rag/provider.py`, `classify_provider_failure`:

```python
if isinstance(error, openai.APIResponseValidationError):
    return ProviderFailure("upstream_invalid_response", True, status, False)
```

This branch precedes generic HTTP status classification, so a malformed
successful response does not become deterministic `provider_rejected`.
`PageIndexRuntime._retryable` delegates to this classification after its
PageIndex-specific cases, so sync and async PageIndex calls follow the same
policy.

### Provider-blocking failures are sticky

`apps/api/src/vectorless_rag/provider.py`, `ProviderGate.status`:

```python
if available:
    self._clear_hard_failure()
    return await self.persist_observation(
        session,
        state,
        ProviderAvailability.available,
```

The LLM and PageIndex completion paths have no success-state mutation. Their
regressions inject a hard failure while a provider callback is in flight, then
return a successful response and prove the failure remains. Only a successful
fresh status/balance probe calls `_clear_hard_failure`.

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

### The coordinator is bounded and is the only trial-result appender

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
new scheduling while admitted work drains. The executor, judgment recorder, and
claim-score updater all hold `evaluation_ledger_lock(run)` around ledger
mutations.

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

### Reservations bind the admitted request

`apps/api/src/vectorless_rag/usage.py`, `provider_input_token_limit`:

```python
def provider_input_token_limit(payload: str | bytes) -> int:
    encoded = payload.encode() if isinstance(payload, str) else payload
    return min(
        DEEPSEEK_MAX_INPUT_TOKENS,
        max(1, len(encoded) + PROVIDER_REQUEST_BYTE_OVERHEAD),
    )
```

`RESERVE_MODEL_CALL_SQL` stores that input limit, the configured output limit,
and the retry limit on the reservation. Reusing a logical call with different
bounds raises `UsagePersistenceError`. A call attached to an invalid experiment
or incompatible reservation returns a rejection sentinel, so ingestion cannot
silently continue unreserved. Terminal reconciliation uses measured usage where
available and retains one immutable per-attempt bound for each attempt without
usage. Reserved and measured costs round upward to the schema’s micro-dollar
precision, so quantization cannot understate cap exposure.

Schema head `0006_bound_cost_reservations` backfills historical rows from their
experiment snapshots. It does not rewrite historical `reserved_cost_usd`,
status, or measured cost. Snapshot limits are validated as positive integral
JSON numbers and cast through `numeric`, which accepts the actual frozen
representation (`1000000.0`, `8192.0`, and `3.0`). The migration refuses
inconsistent historical rows. Snapshots without those keys retain the
historical provider-limit fallbacks (`1000000`, `384000`, and `3`); that legacy
case is separate from the current experiment’s explicit `8192.0` output limit.
The migration then adds a unique
`(cost_reservation_id, provider_attempt)` constraint and a trigger that enforces
the reservation’s logical call, attempt bound, and experiment owner. Separate
triggers make reservation identity and bounds immutable and prevent changing an
ingestion job’s experiment after its first attempt.

`apps/api/alembic/versions/0006_bound_cost_reservations.py`,
`agent.cost_reservation_identity_immutable` and
`agent.ingestion_job_experiment_immutable`:

```sql
IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
   OR NEW.logical_call_id IS DISTINCT FROM OLD.logical_call_id
   OR NEW.input_token_limit IS DISTINCT FROM OLD.input_token_limit
   OR NEW.output_token_limit IS DISTINCT FROM OLD.output_token_limit
   OR NEW.attempt_limit IS DISTINCT FROM OLD.attempt_limit THEN
  RAISE EXCEPTION 'cost reservation identity and bounds are immutable';
END IF;
```

```sql
IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
   AND EXISTS (
     SELECT 1 FROM agent.ingestion_attempts a WHERE a.job_id = OLD.id
   ) THEN
  RAISE EXCEPTION
    'ingestion job experiment binding is immutable after its first attempt';
END IF;
```

### Claim recall has a strict scorer

`apps/api/scripts/execute_evaluation.py`, `Executor.score_terminal_run`:

```python
if trial.experiment_kind == "forced_route_end_to_end" and case.answerable:
    judge = ClaimJudgeItem(
        trial_id=trial.trial_id,
        item_id=case.item_id,
        claims=[claim.claim for claim in case.atomic_claims],
        answer=run.answer or "",
    )
```

`apps/api/scripts/judge_evaluation.py`, `_apply_judgments_unlocked`:

```python
if judgment.item_id != item.item_id or judgment.claims != item.claims:
    raise ValueError(f"claim judgment does not match queued content: {trial_id}")
if judgment.answer_sha256 != item.answer_sha256:
    raise ValueError(f"claim judgment does not match queued answer: {trial_id}")
score = scores_by_trial.get(trial_id)
if score is None:
    raise ValueError(f"claim judgment has no durable score: {trial_id}")
```

The script rejects duplicate or stale trial data, records reviewer provenance in `claim-judgments.jsonl`, and atomically rewrites `scores.jsonl`. Reapplying the same judgments is idempotent.

`ClaimJudgment.answer_sha256` must match `ClaimJudgeItem.answer_sha256`, so a
judgment cannot be replayed against changed answer text. `record_judgment` and
`append_trial_result` both hold `evaluation_ledger_lock(run)` across their full
read-modify-write sequence; the concurrency regression pauses a score rewrite,
attempts an executor append, and proves both records survive.

### Artifact identity includes Python builder behavior

`apps/api/src/vectorless_rag/pageindex_artifact.py`, `build_manifest_v2`:

```python
configuration = {
    "artifact_builder_version": ARTIFACT_BUILDER_VERSION,
    "generator_commit": generator_commit,
    "patch_sha256": patch_sha256,
    "model": model,
```

`apps/api/src/vectorless_rag/pageindex_adapter.py`, `artifact_version_key`:

```python
value = "\0".join(
    (
        pdf_sha256,
        pageindex_version,
        prompt_version,
        model_name,
        artifact_builder_version,
    )
)
```

`_unmapped_ranges` now collects every node’s `direct_page_ranges` recursively.
It no longer treats a widened parent-to-child span as direct coverage. The
builder version participates in both the manifest configuration and reuse key.

### The corrected run requires a new experiment

Current immutable state:

```text
experiment_id = 09471839-de04-4f94-97aa-a34e31b86509
maximum_cost_usd = 25.000000
```

Target transition after committing, rebuilding, and migrating the corrections:

```bash
test ! -e /tmp/pageindex-evaluation-run-09471839
mv apps/api/evaluation/run /tmp/pageindex-evaluation-run-09471839
make evaluation-run \
  INDEX_HASH=ae4a7407c9ec07b60ce580a2e547c4ebd3d40ecae7e4cb3f0bc0f76346a4ca5a \
  MAX_COST=25 CONCURRENCY=4
make evaluation-execute DRY_RUN=1
make evaluation-execute LIMIT=12
```

That command must create a new experiment UUID and clean manifest. Do not edit
the existing JSON or `agent.experiments.maximum_cost_usd`: either mutation
breaks the frozen-protocol claim. Rejected alternatives are raising the current
row in place and copying old scores into a differently bounded experiment. Keep
the `$25` ceiling unless corrected-run evidence proves it is insufficient.
