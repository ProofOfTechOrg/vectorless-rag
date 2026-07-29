<!-- Content type: How-to -->
<!-- Content plan: Goal: validate, review, seed, run, and report the tracked evaluation suites. Audience: retrieval researchers and maintainers. Content: deterministic checks, human review, experiment freezing, reports, and browser coverage. Open questions: sealed vector-versus-PageIndex evaluation results. -->

# Run evaluation workflows

The repository has two evaluation surfaces with different claims.

`apps/api/evaluation/release-v1.jsonl` remains a route, SQL, citation-policy, security, and deterministic browser regression suite. It is not a content-retrieval comparison.

`apps/api/evaluation/pageindex-v2-regression.jsonl` contains 120 source-grounded PageIndex cases: 48 development and 72 regression cases. The six 20-case strata cover explicit single-document, unconstrained discovery, metadata-constrained discovery, long/cross-section, cross-document, and unanswerable requests. The suite is visible to future vector development, so `regression` is deliberate; it is not held out.

## Validate and review

These commands require no PDFs, provider, database, or Langfuse:

```bash
make evaluation-check
make evaluation-review-pack
```

Validation enforces the strict schema, stable UUIDv5 item IDs, 48/72 split, six strata, target-document isolation between splits, corpus membership, page bounds, source hashes, at least 24 vocabulary-mismatch cases, at least 12 exact-phrase cases, and deterministic trial/bootstrap generation.

All 120 tracked review records start as `pending`. A human must approve, edit, or reject every case against `apps/api/evaluation/review-pack.md`. A completed record requires reviewer identity, timestamp, and the current source hash. Seeding and experiment preparation fail closed while any case lacks a completed review. Do not bulk-mark cases reviewed.

Review interactively rather than editing the JSONL by hand:

```bash
make evaluation-review-status
make evaluation-review REVIEWER=your_reviewer_name_here
make evaluation-review REVIEWER=your_reviewer_name_here STRATUM=long_cross_section
```

Each case prints with its source excerpts and accepts approve, edit reference, edit prompt, reject, or skip. The reviewer recomputes the review source hash after an edit, and the stable item ID when an edit changes the prompt, then refuses any edit the suite validator would reject. Editing a prompt changes the item ID, so rebuild the review pack afterwards. Rejected cases fail the run preflight and must be corrected or replaced.

## Rebuild the PageIndex suite

`apps/api/scripts/build_pageindex_evaluation.py` rebuilds all 120 cases from the tracked 537-PDF corpus. It selects source excerpts by discarding page furniture — captions, running headers, reference entries, pseudocode, appendix exemplars, sampled model output, and sentences broken across a line by hyphenation.

Two strata, `metadata_constrained_discovery` and `long_cross_section`, require prompts that restate the finding in different words. String manipulation cannot produce genuine paraphrase, so their text lives in `apps/api/evaluation/pageindex-v2-authored.json`, keyed `split:stratum:index` and bound to its source excerpts by SHA-256. When an entry is missing, malformed, or bound to excerpts that have since changed, the build writes every affected case to `apps/api/evaluation/pageindex-v2-authoring-queue.json` and exits without touching the dataset. Author the queued prompts and reference answers into the authored file, then build again.

Rebuilding carries forward completed reviews whose case content is unchanged. A case whose content changed returns to `pending`, because the reviewer approved different text.

## Seed and freeze an experiment

After review:

```bash
make evaluation-seed
make evaluation-freeze
make evaluation-run INDEX_HASH=sha256_from_evaluation_freeze MAX_COST=25 CONCURRENCY=4
```

`MAX_COST` is the whole run's ceiling in US dollars. It is recorded in the frozen manifest and materialised as the experiment's database limit before the first trial, so execution stops with `experiment_cost_cap` rather than running past the number you chose. Resuming reuses the existing limit rather than raising it, so a resumed run cannot exceed what the plan was frozen with.

Freezing requires the stack running and the corpus ingested, since it reads what is actually queryable. `make evaluation-freeze` reduces every ready document's active PageIndex artifact digest to one value, ordered by arXiv ID, the same construction the corpus manifest uses for PDFs. It fails when any document the corpus manifest targets is missing from the index, because an unindexed target scores as a retrieval miss rather than as the setup error it is.

The digest covers the whole ready catalog rather than only the targeted documents. Corpus routing searches everything, so ingesting or reindexing an unrelated document changes what the router chooses between and therefore changes the measurement. Re-freeze after any ingestion, and treat a changed digest as a different index: results from two digests are not comparable.

Seeding uses each case’s stable UUID as the Langfuse dataset item ID. The run preflight requires a clean worktree, exact release commit, complete provider price snapshot, reviewed dataset, tracked corpus hash, and explicit frozen index hash. It writes ignored local artifacts under `apps/api/evaluation/run/`:

- `manifest.json`: release, configuration, corpus, index, dataset, prices, budgets, repetitions, execution concurrency, seed, and cost ceiling
- `trials.jsonl`: fixed-seed interleaving for route, oracle-document retrieval, and forced-route end-to-end experiments
- `scores.jsonl`: trial resume ledger and local score input
- `judge-queue.jsonl`: answer text and atomic claims awaiting claim-recall review
- `claim-judgments.jsonl`: reviewer decisions and provenance, bound to the answer digest
- `summary.json`: aggregate output

The plan contains 1,080 trials: three experiment types, three evidence budgets (6,400, 12,800, and 25,600 tokens), one repetition, and 120 items. Pass `--repetitions 2` or `3` to estimate within-configuration variance at proportional provider cost; the manifest records the count, so a single-pass run is never mistaken for a repeated one. Historical manifests may still contain `corpus_routing`, but new plans omit it because it executed the same graph as `forced_route_end_to_end`.

## Execute the plan

`make evaluation-run` only prepares the plan. Execute it against the configured provider:

```bash
make evaluation-execute DRY_RUN=1
make evaluation-execute LIMIT=10
make evaluation-execute
```

The dry run resolves every target arXiv ID to an indexed, ready document and fails closed if any is missing, without issuing a provider call. Always run it first: a missing index makes a trial unscoreable rather than merely wrong.

Execution drives `GraphRunner` in process, because the protocol needs per-trial control of the forced route, the oracle document set, and the evidence budget, and none of those are fields on `ChatRequest`. Each experiment kind maps onto one override: `route` leaves the router free so its choice is measured; `forced_route_end_to_end` pins the route so retrieval and answer generation are measured without routing noise in front of them; `oracle_document_retrieval` additionally supplies the target documents so within-document node selection is isolated.

`scores.jsonl` is keyed by trial ID, so an interrupted execution resumes without
repeating provider work. The executor appends trial results in frozen plan
order. Claim judging later replaces the file atomically to add
`answer_claim_recall`; both writers share one filesystem lock, so neither can
erase the other’s work. The manifest freezes execution concurrency, which
defaults to four and may be set from one through eight with `CONCURRENCY` when
the plan is created. Each worker owns an isolated database session and LangGraph
checkpointer connection. `LIMIT` caps how many pending trials a single
invocation executes. Provider failures are recorded with `included: false` and
a failure code rather than aborting the run, so one bad trial does not discard
the rest.

The database cost trigger serializes reservations against the experiment row.
Once one worker reaches the ceiling, the coordinator schedules no new trials
and lets only calls whose worst-case cost was already reserved finish.
Cancellation terminalizes each in-flight query run before propagating, so a
resume reconstructs its excluded score instead of repeating provider work.

Route, document, support, and citation measurements are computed from the
returned citations. Claim recall is not: judging whether an answer asserts each
atomic claim requires reading both, so answerable end-to-end trials enter
`judge-queue.jsonl`. Record and apply judgments with:

```bash
make evaluation-judge-status
make evaluation-judge REVIEWER=your_reviewer_name_here
```

Use `METHOD=automated` only when that provenance is accurate. Each judgment
records one decision per atomic claim and binds the trial, item, claims, and
answer SHA-256. Applying the same judgment again is idempotent; conflicting or
stale content fails closed.

Run `make evaluation-report` after claim review, or earlier for a partial report
whose claim-recall count is zero. The report rejects duplicate or unknown trial
IDs and summarizes route, document, support, claim, citation, latency, retry,
and failure measurements with deterministic bootstrap intervals.

A final unbiased vector-versus-PageIndex architecture verdict still requires a new sealed dataset created only after both implementations and their indexes freeze.

## Browser verification

`make playwright` starts a local mock upstream while retaining the real TanStack UI, server functions/routes, encrypted cookie, authorization gates, multipart proxy, and SSE parser. The mock fragments SSE and covers stable browser states without provider spend. `make playwright-live` is the low-volume credential-safe integration path in the operator guide.
