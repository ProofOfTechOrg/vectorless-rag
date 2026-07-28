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
make evaluation-review REVIEWER=<your identity>
make evaluation-review REVIEWER=<your identity> STRATUM=long_cross_section
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
make evaluation-run INDEX_HASH=<frozen-index-sha256>
```

Seeding uses each case’s stable UUID as the Langfuse dataset item ID. The run preflight requires a clean worktree, exact release commit, complete provider price snapshot, reviewed dataset, tracked corpus hash, and explicit frozen index hash. It writes ignored local artifacts under `apps/api/evaluation/run/`:

- `manifest.json`: release, configuration, corpus, index, dataset, prices, budgets, repetitions, and seed
- `trials.jsonl`: fixed-seed interleaving for route, corpus-routing, oracle-document retrieval, and forced-route end-to-end experiments
- `scores.jsonl`: append-only local score input
- `summary.json`: aggregate output

The plan contains 4,320 trials: four experiment types, three evidence budgets (6,400, 12,800, and 25,600 tokens), three repetitions, and 120 items.

Run `make evaluation-report` after populating `scores.jsonl`. The report rejects duplicate or unknown trial IDs and summarizes route, document, support, claim, citation, latency, retry, and failure measurements with deterministic bootstrap intervals.

A final unbiased vector-versus-PageIndex architecture verdict still requires a new sealed dataset created only after both implementations and their indexes freeze.

## Browser verification

`make playwright` starts a local mock upstream while retaining the real TanStack UI, server functions/routes, encrypted cookie, authorization gates, multipart proxy, and SSE parser. The mock fragments SSE and covers stable browser states without provider spend. `make playwright-live` is the low-volume credential-safe integration path in the operator guide.
