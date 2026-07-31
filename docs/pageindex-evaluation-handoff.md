<!-- Content type: Reference -->
<!-- Content plan: Goal: preserve the completed PageIndex v2 evaluation, its accounting evidence, and the route correction it motivated. Audience: maintainers continuing PageIndex retrieval work. Content: immutable baseline, final results, interpretation, defect history, route correction, cost audit, artifacts, reuse map, verification, and remaining limits. Open questions: provider-side reconciliation for unknown-usage calls and empirical validation of the revised route policy if separately authorized later. -->

# PageIndex v2 evaluation results and handoff

The PageIndex v2 evaluation is complete. Do not rerun it or increase its token
budget to continue this investigation. The current checkout fixes the route
defects exposed by the completed run. No replacement experiment is authorized
or planned.

Branch `fix/pageindex-evaluation-dataset` at base commit
`97b5dca3b4c622a4e6030b978f5f3532ef857e10` is the route-correction baseline.
The evaluated runtime remains release
`d32f7a36c18a6818fbebce24582fc175107c8b9f`, with PostgreSQL at schema head
`0009_atomic_model_call_start`. Reconfirm these anchors against the checkout
before editing; line numbers are intentionally omitted because they drift.

## Final state

| Field | Value |
| --- | --- |
| Experiment | `2937b0a7-bbbe-47e1-b0dd-f74b8587efbc` |
| Runtime release | `d32f7a36c18a6818fbebce24582fc175107c8b9f` |
| Configuration | `897ea99deb6c9ef0b9ecd9d298ce187e86a712aa0b856989f2da18c02bcac9da` |
| Reviewed corpus | `d7e2d075f9e20c4778269bf4de6b7b4d9158dc92e206eaeccf59a25954442b68` |
| Frozen index | `ae4a7407c9ec07b60ce580a2e547c4ebd3d40ecae7e4cb3f0bc0f76346a4ca5a` |
| Dataset | `821e973c6362b105e899586cac9c9c189c6deb98cf0de047a8a9b3f754a9f872` |
| Plan | 1,080 trials, one repetition, budgets 6,400/12,800/25,600 |
| Execution | Concurrency 4; 12-trial canary followed by 1,068 trials |
| Scores | 1,080 unique; 1,018 included; 62 excluded |
| Claim judgments | 269 items; 365 claims; 153 supported; 212 unsupported |
| Ceiling | `$21.360000` |
| Accounting exposure | `$5.389689` |
| State | Complete; zero nonterminal owners, started calls, or reserved reservations |

The provider-free dry run resolved all 104 indexed documents before the
canary. The canary produced 12 included scores, 69 completed model calls, no
started calls, no reserved or review-required reservations, and `$0.056017`
exposure. The remaining trials were allowed to run only after that audit.

## Result

The full report is the ignored local file
`apps/api/evaluation/run/summary.json`. Means below are over included trials;
the report contains deterministic 95% bootstrap intervals and counts.

### Free route selection

Every suite case requires the `documents` route. A free-route trial is correct
only when the graph chooses that route.

| Evidence budget | Dev route correctness | Regression route correctness |
| ---: | ---: | ---: |
| 6,400 | 0.238 | 0.188 |
| 12,800 | 0.317 | 0.303 |
| 25,600 | 0.238 | 0.271 |

Forced-route and oracle trials have route correctness 1.0 by construction.
The free router often chose `hybrid` or `clarify` for prompts that the reviewed
suite defines as document questions. That choice occurs before PageIndex
evidence selection, so a larger evidence window cannot repair it.

Free-route refusal correctness is also poor: 0.000–0.250 across the six
split/budget groups. Forced-route and oracle refusal correctness is 1.0 in
every group.

### Forced-route end-to-end quality

| Budget | Split | Claim recall | Document recall | Support recall | Citation precision |
| ---: | --- | ---: | ---: | ---: | ---: |
| 6,400 | dev | 0.471 | 0.529 | 0.441 | 0.441 |
| 6,400 | regression | 0.509 | 0.582 | 0.436 | 0.433 |
| 12,800 | dev | 0.500 | 0.531 | 0.438 | 0.422 |
| 12,800 | regression | 0.421 | 0.544 | 0.395 | 0.395 |
| 25,600 | dev | 0.419 | 0.486 | 0.351 | 0.351 |
| 25,600 | regression | 0.343 | 0.481 | 0.333 | 0.333 |

The regression claim-recall mean falls from 0.509 at 6,400 tokens to 0.343 at
25,600. The dev means and their intervals do not establish a benefit from the
larger windows. A budget increase is therefore not justified.

The judgment ledger covers all 269 included answerable end-to-end trials:

| Claim recall | Trials |
| ---: | ---: |
| 0.0 | 146 |
| 0.5 | 10 |
| 1.0 | 113 |

The mean across those trials is 0.438662. Judgments are bound to the answer
digest and record reviewer `codex`, method `automated`. Exact insufficient-
evidence answers were marked unsupported. Substantive quotes and paraphrases
were marked supported; empty, title-only, mismatched, or partially omitted
atomic claims were marked unsupported.

### Oracle document retrieval

| Budget | Dev document recall | Regression document recall |
| ---: | ---: | ---: |
| 6,400 | 0.675 | 0.633 |
| 12,800 | 0.550 | 0.627 |
| 25,600 | 0.525 | 0.700 |

The regression split improves at 25,600, but dev moves in the opposite
direction. With one repetition and overlapping bootstrap intervals, this does
not support a global budget increase.

### Exclusions

| Failure | Count | Interpretation |
| --- | ---: | --- |
| `provider_rejected` | 55 | Contained non-retryable provider response |
| `TimeoutError` | 4 | Expected terminal failure at the exact 180-second deadline |
| `generated_sql_rejected` | 3 | Malformed model SQL contained at the database adapter |

The experiment has 1,080 terminal memberships and no nonterminal owner.
Across 5,206 model calls, 5,067 completed, 55 failed non-retryably, 55 failed
on retryable network or structured-output errors, and 29 were cancelled
siblings. No call is `started` or `abandoned`, and no call has
`usage_persistence_failed`.

## Accounting

The 5,151 cost reservations are terminal:

| Reservation state | Count | Exposure |
| --- | ---: | ---: |
| `finalized` | 5,027 | `$3.406864` |
| `review_required` | 124 | `$1.982825` |
| `reserved` | 0 | `$0.000000` |
| Total | 5,151 | `$5.389689` |

Known measured usage is `$3.426465`. That figure includes `$0.019601`
measured on review-required rows. The cap exposure deliberately charges each
review-required row at its conservative `$1.982825` reservation total instead
of trusting incomplete usage.

Do not release or finalize those 124 holds from inference. Reconcile them only
from provider-side usage evidence. They correspond to calls where a response,
network attempt, or cancelled sibling may have consumed tokens without final
usage telemetry.

The authorization sequence began with `$3.632026` exposure across the earlier
July 30 experiments. Adding this experiment produces `$9.021715` cumulative
accounting exposure, leaving `$15.978285` below the authorized `$25` cap. This
is ledger exposure, not a claim about the provider invoice.

The experiment was frozen at `$21.36` so its worst case plus earlier exposure
could not exceed `$25`. Its actual exposure never approached that ceiling. No
budget increase was needed.

## Defects found and fixed

Three releases were required after the first corrected dry run:

| Commit | Root cause | Correction and evidence |
| --- | --- | --- |
| `498c86b245d3951abfaa4b72c2b7cb02e3756dc4` | Reservation and `ModelCall(outcome=started)` creation were separate interruption points | Migration `0009_atomic_model_call_start` makes admission and call start atomic and adds terminal-owner recovery |
| `5274517d7cd05c01bcece24582fc175107c8b9f` | Provider telemetry classified API connection errors as non-retryable while a second classifier retried them | One provider classifier now owns retryability; network and timeout errors are retryable, and the duplicate transient list is gone |
| `d32f7a36c18a6818fbebce24582fc175107c8b9f` | PostgreSQL invalid-regex errors surfaced as SQLAlchemy `DataError`, outside the graph’s `ProgrammingError` catch | `PostgreSQLAdapter` translates generated-query data/programming errors to `SQLRejected` while infrastructure SQLSTATEs still propagate |

The final run exercised the SQL fix three times as
`generated_sql_rejected` and continued. It also passed the exact point where
the preceding release crashed on an unbalanced model-generated PostgreSQL
regular expression.

Earlier prerequisite fixes remain part of the baseline: concurrent compact
catalog routing, shared runtime routing concurrency, request-sized immutable
reservations, database-enforced reservation identity, terminal unknown-attempt
reconciliation, duplicate graph-plan removal, answer-bound claim judgments,
and a shared evaluation-ledger lock.

## Retired runs

Do not combine these prefixes with the final report.

| Experiment | Why retired | Final exposure | Local archive |
| --- | --- | ---: | --- |
| `ad130fcf-df2b-480e-bac1-ea8382dae5ee` | Interrupted before atomic start/recovery fix | `$0.307880` | `/tmp/pageindex-evaluation-run-ad130fcf-df2b-480e-bac1-ea8382dae5ee` |
| `de049d52-80d1-4a48-900f-fe48ed00dcc4` | Retry-classification split caused `usage_persistence_failed` | `$0.083613` | `/tmp/pageindex-evaluation-run-de049d52-80d1-4a48-900f-fe48ed00dcc4` |
| `d9ac61a5-b0c6-4ebc-890e-afc422e07a78` | Generated invalid SQL escaped containment at 389 scores | `$2.804100` | `/tmp/pageindex-evaluation-run-d9ac61a5-b0c6-4ebc-890e-afc422e07a78` |
| `09471839-de04-4f94-97aa-a34e31b86509` | Old 1,440-trial plan stopped at its `$25` ceiling | `$24.290311` | Historical database state |
| `4b1098f7-81d8-4d7b-a6e5-7b7da6dcc60b` | Sequential execution was operationally impractical | `$8.449869` | `/tmp/pageindex-evaluation-run-4b1098f7` |

The `$24.290311` historical exposure was mostly a `$23.051574` hold produced
by the old million-input-token reservation policy. It was not equivalent to
provider spend and must not be extrapolated into a `$155` budget request.

## Artifact identity

`apps/api/evaluation/run` is intentionally Git-ignored. Preserve it before
cleaning the workspace. An exact local copy is at
`/tmp/pageindex-evaluation-run-2937b0a7-bbbe-47e1-b0dd-f74b8587efbc`.
The completed files have these SHA-256 digests:

| Artifact | SHA-256 |
| --- | --- |
| `.evaluation-ledger.lock` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `manifest.json` | `57e24b721f95012f9b19a96c953dfa90465a039030885656ca29789222ab4c84` |
| `trials.jsonl` | `39cd4af30d4f03b5582a99860c2c113de9f6594e7fc836ea108229c0f51cbcdd` |
| `scores.jsonl` | `178d5a0becad0200139a092e9eb0ddd7935c4dd16844aadb89b20f342ce59eaa` |
| `judge-queue.jsonl` | `8c016dfa2321a336e4d820443cbfdf9268cc7d794d52f10ad660bdc4544efb77` |
| `claim-judgments.jsonl` | `5e96c7d30afaf5592293d7d20066e3252ede2396f847fc484e18e5eef2b8fd7e` |
| `summary.json` | `8309bc9a045ebefb27246b40ed3c5f2cf6069671e475b3ce5dd59934141028db` |

## Route defects corrected after the experiment

The existing experiment supplied 360 free-route observations. The table groups
the persisted choices across both splits and all three evidence budgets. A
missing route means the provider call failed before a decision was persisted.

| Stratum | `documents` | `hybrid` | `clarify` | Missing |
| --- | ---: | ---: | ---: | ---: |
| Explicit single document | 31 | 29 | 0 | 0 |
| Unconstrained discovery | 4 | 46 | 0 | 10 |
| Metadata-constrained discovery | 1 | 59 | 0 | 0 |
| Long cross-section | 41 | 16 | 3 | 0 |
| Cross-document | 1 | 42 | 0 | 17 |
| Unanswerable | 7 | 1 | 52 | 0 |

These outcomes and the subsequent architecture review exposed five defects in
`GraphRunner`:

- The route request omitted `ChatRequest.constraints`, even though document
  retrieval applied those constraints later. The router could not distinguish
  a preselected document or catalog subset from an unconstrained request.
- The route policy did not give content evidence precedence over output wording.
  The router treated requests that asked which paper contained or reported a
  passage as metadata identification and sent them to `hybrid`.
- The route policy did not distinguish missing intent from missing evidence.
  The router sent complete questions about a deliberately nonexistent
  mechanism to `clarify` instead of retrieval and a grounded
  insufficient-evidence result.
- Structured constraints reached document retrieval but not guarded SQL.
  Filtered SQL answers could ignore API-level author, topic, date, or arXiv
  filters, while hybrid SQL could select documents outside those filters.
- Hybrid treated any nonempty catalog result as sufficient when body retrieval
  failed. Catalog metadata cannot answer a request that the route policy says
  requires page content.

The correction adds every active structured constraint to the route input. The
route policy now chooses by required evidence source. It explicitly assigns
quoted passage lookup, content-based paper discovery, and corpus claim checks
to `documents`. It limits `hybrid` to metadata selection followed by body
reading, and limits `clarify` to requests whose intent remains incomplete after
history resolution. The guarded SQL boundary wraps every catalog reference in
the same structured filters and rejects constrained queries over an
unfilterable summary view. Hybrid now reports insufficient evidence whenever
body retrieval is insufficient, regardless of catalog rows.

Provider-free tests verify the complete constraint payload, the observed route
failure classes, SQL filter enforcement, and hybrid evidence invariants. They
do not measure how frequently the provider follows the revised prompt. The
decision for this work is to stop after fixing and reviewing the defects,
without a new provider-backed experiment.

Do not increase `evidence_token_budget`. Route selection happens before
evidence retrieval, and the completed end-to-end results show no monotonic
benefit from a larger window.

## Remaining work and limits

- Reconcile the 124 `review_required` reservations only from provider-side
  usage evidence.
- Treat the revised route policy as unmeasured by a live provider until a
  separately authorized evaluation runs. Do not infer a new accuracy number
  from provider-free tests.
- Keep the completed manifest and result artifacts immutable. Any later live
  comparison requires a new experiment identity because the release changed.
- A final vector-versus-PageIndex verdict still requires the sealed comparison
  described in `docs/evaluation.md`.

## Reuse map

Extend these surfaces instead of creating a parallel evaluation or accounting
path:

| Responsibility | Existing surface |
| --- | --- |
| Route policy and graph transitions | `apps/api/src/vectorless_rag/graph.py` |
| Corpus routing and concurrency | `apps/api/src/vectorless_rag/retrieval.py`, `CorpusRouter` and `CatalogRoutingLimiter` |
| Frozen schemas, hashes, trials, and intervals | `apps/api/src/vectorless_rag/evaluation_suite.py` |
| Plan creation | `apps/api/scripts/run_evaluation.py` |
| Resume ledger and bounded execution | `apps/api/scripts/execute_evaluation.py` |
| Provider classification and retries | `apps/api/src/vectorless_rag/provider.py` and `apps/api/src/vectorless_rag/llm.py` |
| Cost reservation and recovery | `apps/api/src/vectorless_rag/usage.py` |
| Generated SQL boundary | `apps/api/src/vectorless_rag/sql_guard.py`, `PostgreSQLAdapter` |
| Claim judgments | `apps/api/scripts/judge_evaluation.py` |
| Aggregate report | `apps/api/scripts/report_evaluation.py` |
| Operator commands | Root `Makefile`, targets prefixed `evaluation-` |

## Deliberately unchanged

- The 104-document index was not rebuilt. Its frozen digest is part of the
  experiment identity.
- The user-owned `arxiv-pdfs` corpus was not moved or modified.
- OCR remains unsupported.
- The 180-second production request deadline remains intentional.
- Provider rejections remain excluded terminal trials, not silently retried
  forever.
- Review-required cost holds remain conservative until external usage evidence
  exists.
- One repetition does not estimate run-to-run provider variance. Do not claim
  that small differences between overlapping intervals are definitive.

## Verification record

The behavior changes in the evaluated release and the later route correction
passed:

```text
489 provider-free Python tests
27 isolated PostgreSQL integration tests
Ruff lint and format checks
Basedpyright
migration head check at 0009_atomic_model_call_start
PageIndex evaluation dataset validation
documentation contract validation
```

The integration command was:

```bash
TEST_POSTGRES_PORT=55433 make test-integration-stack
```

The completed evaluation workflow was:

```bash
make evaluation-execute DRY_RUN=1
make evaluation-execute LIMIT=12
make evaluation-execute
make evaluation-judge REVIEWER=codex METHOD=automated
make evaluation-judge-status
make evaluation-report
```

Done means:

- `scores.jsonl` contains 1,080 unique trial IDs;
- `evaluation-judge-status` prints 269 completed and zero remaining;
- `summary.json` reports 1,080 scored, 1,018 included, and 62 excluded;
- PostgreSQL has 1,080 terminal memberships, zero started model calls, zero
  abandoned model calls, and zero reserved reservations;
- accounting exposure is `$5.389689`; and
- artifact hashes match the table above.

## Appendix: current-state anchors

Read these symbols again before editing.

`apps/api/scripts/execute_evaluation.py`, `Executor.score_terminal_run`, defines
the route metric:

```python
route = run.route.value if run.route is not None else ""
score = EvaluationScore(
    trial_id=trial.trial_id,
    query_run_id=run.id,
    route_correct=float(route == EXPECTED_ROUTE.value),
```

All current suite cases expect `RunRoute.documents`; a low free-route score
therefore means the graph chose another route, not that PageIndex selected the
wrong document after routing.

`apps/api/src/vectorless_rag/graph.py`, `GraphRunner.run.route`, now supplies
the request constraints to the router:

```python
route_input = json.dumps(
    {
        "history": state["history"],
        "current_request": state["question"],
        "structured_constraints": request.constraints.model_dump(
            mode="json",
            exclude_defaults=True,
        ),
    },
    ensure_ascii=False,
)
```

`ROUTE_SYSTEM_PROMPT` in the same file defines the corrected precedence. Keep
the following invariants together when editing it:

- Missing evidence is not missing intent.
- Page-content discovery uses `documents`, even when the answer identifies a
  paper.
- Active structured constraints restrict both document retrieval and guarded
  catalog SQL.
- `hybrid` requires metadata selection followed by page-content reading.
- `sql` requires catalog metadata only.

`apps/api/src/vectorless_rag/sql_guard.py`, `validate_sql`, applies active
`MetadataConstraints` after validating the generated query and before
normalizing it. The constraint wrapper must cover every
`agent.paper_catalog_v1` reference. Constrained queries must reject
`agent.ingestion_summary_v1`, because that aggregate view cannot enforce
document-level filters.

`apps/api/src/vectorless_rag/graph.py`, `GraphRunner.run.documents_node`, keeps
an empty hybrid SQL selection as `restrict_ids=[]`. It also preserves
insufficiency when catalog selection succeeds but page-content retrieval does
not. Catalog rows never substitute for the body evidence a hybrid request
requires. A successful hybrid synthesis must cite at least one document
evidence label; a SQL-only citation triggers one repair attempt and then fails
closed.

`apps/api/scripts/execute_evaluation.py`, the same symbol, queues only
answerable end-to-end answers for claim review:

```python
if trial.experiment_kind == "forced_route_end_to_end" and case.answerable:
    judge = ClaimJudgeItem(
        trial_id=trial.trial_id,
        item_id=case.item_id,
        claims=[claim.claim for claim in case.atomic_claims],
        answer=run.answer or "",
    )
```

`apps/api/scripts/report_evaluation.py` counts exclusions independently of
included aggregates:

```python
failures = Counter(
    score.failure_code or "unclassified" for score in scores if not score.included
)
```

`apps/api/src/vectorless_rag/sql_guard.py`, `PostgreSQLAdapter`, is the
generated-query error boundary. It must continue to translate model-caused
data/programming errors to `SQLRejected` while allowing connectivity,
permissions, missing schema/table, lock contention, and non-timeout
operational failures to propagate as infrastructure faults.

The manifest is immutable. Do not edit `experiment_id`, `release_sha`,
configuration/corpus/index/dataset digests, evidence budgets, concurrency, or
`maximum_cost_usd` in place. Create a new experiment for a changed release or
protocol.
