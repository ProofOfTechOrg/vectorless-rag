<!-- Content type: Reference -->
<!-- Content plan: Goal: let a fresh maintainer resume the PageIndex v2 evaluation without redoing its investigation. Audience: maintainers continuing the evaluation. Content: current state, decisions with rationale, diagnosed failure classes, operating recipes, measured costs, and remaining steps. Open questions: MAX_COST value, and whether to merge the branch before executing. -->

# Resume the PageIndex v2 evaluation

State, decisions, and findings for the PageIndex v2 evaluation as of branch
`fix/pageindex-evaluation-dataset`, written against base commit
`f6bbd7240211bab5a36a38b40e3401480ec76204`. Written so the work can continue
from the repository and this page alone. Reconfirm all anchors against the
current checkout before editing.

## Where it stands

| Item | State |
| --- | --- |
| Evaluation dataset | 120/120 cases human-approved; `make evaluation-check` clean |
| Corpus index | 104/104 documents indexed and ready |
| Index recipe | 98 documents on `+vr5`, 6 on `+vr6` — deliberate, see below |
| Trial executor | Live gate complete: 10/1,440 scored, 8 included, 2 deadline exclusions |
| Cost ceiling | Implemented; `make evaluation-run` now requires `MAX_COST` |
| Branch | No remote branch; not merged. Refresh `origin` before comparing refs |

`make api-lint`, `make api-typecheck`, `make docs-check`, and `make compose-check`
pass. The provider-free API suite is 398 passed; the disposable PostgreSQL
integration stack is 9 passed.

## Do this next

The frozen run is already prepared, armed, and resumable. Continuing it sends
evaluation prompts and retrieved corpus excerpts to DeepSeek. Obtain explicit
approval for that full 1,430-trial egress, then run:

```bash
make evaluation-execute DRY_RUN=1
make evaluation-execute
make evaluation-report
```

The dry run is safe to repeat and should report 10 already scored and 1,430
pending without provider calls. Do not create a new manifest: that would create
a different experiment rather than resume this one.

## Current frozen run

| Field | Value |
| --- | --- |
| Experiment | `4b1098f7-81d8-4d7b-a6e5-7b7da6dcc60b` |
| Runtime release | `0dd3bbb94851f53cfec6b4dfe16915afb79a6a35` |
| Runtime configuration | `ea661c92dd99001b1faf152f50349142512c04e2c33ee614de394a33862a1656` |
| Reviewed corpus | `d7e2d075f9e20c4778269bf4de6b7b4d9158dc92e206eaeccf59a25954442b68` |
| Active index | `ae4a7407c9ec07b60ce580a2e547c4ebd3d40ecae7e4cb3f0bc0f76346a4ca5a` |
| Dataset | `821e973c6362b105e899586cac9c9c189c6deb98cf0de047a8a9b3f754a9f872` |
| Ceiling | `$25.000000` |
| Progress | 10 scored: 8 included, 2 excluded as `TimeoutError` |
| Model accounting | 47 calls, `$0.093061` measured, `$0` actively reserved |

Both exclusions are 12,800-token cross-document catalog workloads. They
completed four to seven large document-selection calls, cancelled the next call
at the exact 180-second production deadline, persisted
`failure_code=deadline_exceeded` plus `error=TimeoutError`, reconciled every
reservation, and allowed the next trial to continue. This is measured runtime
behaviour, not an executor failure.

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
- **Trials: $0.009306/trial over the 10-trial live gate** — 47 model calls cost
  $0.093061. A straight-line projection is about $13.40 for 1,440 trials, but
  this small shuffled sample includes two expensive deadline cases and is not a
  forecast. `MAX_COST=25` remains the authoritative bound.

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
cd apps/api && uv run pytest          # expect 398 passed, 9 skipped
TEST_POSTGRES_PORT=55433 make test-integration-stack  # expect 9 passed
make pageindex-patch-test PAGEINDEX_SOURCE=/opt/pageindex
```

A final unbiased vector-versus-PageIndex verdict still needs a new sealed dataset
created after both implementations and their indexes freeze. This suite is a
PageIndex v2 regression baseline, and it is visible to future vector development.
