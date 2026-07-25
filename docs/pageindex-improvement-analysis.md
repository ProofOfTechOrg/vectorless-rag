<!-- Content type: Conceptual and reference -->

# Harden PageIndex before comparing retrieval architectures

This record defines the PageIndex v2 implementation baseline, architecture, rollout, and verification evidence. It is the durable handoff for the work that starts at commit `cfbea604c64d51c20ca1cea413b2bfa35be94471`.

## Baseline

The implementation starts from this frozen repository and corpus state:

| Item | Value |
|---|---|
| Branch | `master` |
| Base commit | `cfbea604c64d51c20ca1cea413b2bfa35be94471` |
| Worktree at start | Clean |
| Git remote | None configured |
| PageIndex upstream | `190f8b378be58199ca993566a9214dba72089c54` |
| Local PageIndex patch | `+vr3` |
| Corpus | 537 PDFs, 12,086 pages |
| Historical ingestion attempts | 17 total, 10 completed, 7 failed |
| Historical model calls | 978 |
| Historical measured tokens | 1,930,266 |
| Historical known provider spend | $0.767451 |
| Historical artifacts | Ten schema-v1 flat artifacts, 227 nodes, 436 overlapping range pairs |

The existing vector RAG comparison remains provisional until both arms use frozen full-corpus indexes and a sealed evaluation dataset created after both implementations freeze.

## Current-state anchors

These anchors are verbatim from the base commit. Re-read each symbol before editing because line numbers change after the first patch.

### Hierarchy is flattened

`apps/api/src/vectorless_rag/pageindex_adapter.py::_walk_nodes` discards parent-child relationships:

```python
if isinstance(children, list):
    result.extend(_walk_nodes(cast(list[dict[str, Any]], children)))
```

`apps/api/src/vectorless_rag/pageindex_adapter.py::normalize_artifact` stores the flat tree and every page in one mutable object shape:

```python
return {
    "schema_version": 1,
    "metadata": metadata,
    "description": description,
    "tree": normalized_nodes,
    "pages": [{"page": index + 1, "text": text} for index, text in enumerate(pages)],
}
```

### Reuse happens after paid metadata extraction

`apps/api/src/vectorless_rag/ingestion.py::IngestionProcessor.process` computes metadata before checking the version key:

```python
metadata, page_count, quality = await extract_metadata(
    pdf_data, document.original_filename, self.llm, self.settings
)
version_key = artifact_version_key(
    document.sha256,
    self.settings.pageindex_version,
    self.settings.metadata_prompt_version,
    self.settings.pageindex_model,
)
existing = await session.scalar(
    select(IndexVersion).where(
        IndexVersion.document_id == document.id,
        IndexVersion.version_key == version_key,
    )
)
```

The same method overwrites the artifact identity for forced builds:

```python
artifact_key = f"artifacts/{document.id}/{version_key}.json"
await self.store.put_bytes(artifact_key, artifact, "application/json")
index = existing or IndexVersion(
    document_id=document.id,
    version_key=version_key,
    pageindex_version=self.settings.pageindex_version,
    prompt_version=self.settings.metadata_prompt_version,
    model_name=self.settings.pageindex_model,
    artifact_object_key=artifact_key,
    artifact_sha256=artifact_digest,
)
index.artifact_object_key = artifact_key
index.artifact_sha256 = artifact_digest
```

### Corpus routing uses lexical exclusion

`apps/api/src/vectorless_rag/retrieval.py::CorpusRouter.candidates` truncates the eligible catalog:

```python
.limit(self.settings.candidate_limit)
...
if len(matched) >= self.settings.candidate_limit:
    return candidates
```

`apps/api/src/vectorless_rag/retrieval.py::PageIndexRetriever.retrieve` omits the stored description and hierarchy:

```python
{
    "document_id": str(document.id),
    "title": document.title,
    "tree": tree,
}
```

`apps/api/src/vectorless_rag/retrieval.py::PageIndexRetriever._fetch_evidence` double-counts overlapping pages and preserves the pre-truncation locator:

```python
pages_used = sum((item.page_end or 0) - (item.page_start or 1) + 1 for item in existing)
...
if len(encoded) > remaining_tokens:
    content = self.encoding.decode(encoded[:remaining_tokens])
    encoded = encoded[:remaining_tokens]
...
page_end=end,
```

### Child execution inherits host secrets

`apps/api/src/vectorless_rag/pageindex_adapter.py::PageIndexAdapter.index` copies the full process environment:

```python
environment = os.environ.copy()
```

### Provider and citation failures lose meaning

`apps/api/src/vectorless_rag/pageindex_runtime.py` collapses provider rejection:

```python
raise LLMError(
    "llm_provider_rejected",
    "The model provider rejected the request.",
    attempts=attempt,
)
```

The same runtime accepts this semantic fallback:

```python
invalid_json_fallback={"answer": "no"},
```

`apps/api/src/vectorless_rag/graph.py::format_inline_citations` fabricates citation placement:

```python
missing = [marker for marker in markers if marker not in rendered]
if not missing:
    return rendered
return f"{rendered.rstrip()} {' '.join(missing)}"
```

### Evaluation prompts expose target identity

`apps/api/scripts/build_evaluation_dataset.py` creates a target-revealing prompt:

```python
f"What is the principal contribution of ArXiv {identifier}? Cite exact pages."
```

## Target architecture

PageIndex v2 keeps the generator’s nested hierarchy and separates immutable raw output, page shards, and a manifest. PostgreSQL records every build as a version and changes the active pointer only after all objects validate.

The object layout is content addressed:

```text
artifacts/v2/raw/{prefix}/{sha256}.json
artifacts/v2/pages/{prefix}/{sha256}.json.gz
artifacts/v2/manifests/{prefix}/{sha256}.json
```

The retrieval architecture separates catalog selection from evidence retrieval. Catalog selection reasons over every eligible document in deterministic token-bounded batches. Lexical rank and exact matches remain signals but never exclude a document. Evidence retrieval expands nested trees for at most eight selected documents, tracks prior choices and missing evidence, unions page intervals, and stops after three rounds or no progress.

The ingestion architecture uses short transactions and renewable leases. It checks reusable metadata and indexes before downloading source bytes or invoking a model. Build work occurs outside a database transaction. Activation validates the job, attempt, worker, and lease token before switching the document pointer and appending an activation audit.

Provider admission control is shared across ingestion, query, and evaluation paths. It records only categorical state and safe failure metadata. Budgeted experiments reserve worst-case call cost before starting provider work.

## Locked decisions

The implementation follows these decisions:

- Preserve raw history and classify invalid measurements through append-only events
- Classify four historical attempts as insufficient-credit incidents and three as pre-fix code defects
- Use complete-catalog vectorless document reasoning by default
- Treat lexical search as a ranking signal, not a recall boundary
- Keep the self-hosted open-source PageIndex arm free of embeddings
- Run a 25-PDF v2 pilot under a $10 combined provider-cost ceiling
- Commit 48 development and 72 regression cases as a visible regression suite
- Never describe the 72 committed regression cases as held out
- Keep `/health/ready` infrastructure-only
- Preserve schema-v1 artifacts through a read-only compatibility path

## Rejected alternatives

The following options are rejected:

- **Delete or reset history**: this removes real spend and prevents audit reconciliation
- **Classify all seven failures as credit exhaustion**: three failures have complete usage and match a fixed invalid-output defect
- **Keep the top-50 lexical gate**: relevant papers can remain invisible to PageIndex reasoning
- **Mutate artifacts in place**: concurrent reads, rollback, and benchmark reproduction require immutable objects
- **Fail readiness on credit state**: non-model surfaces remain healthy and must not enter restart loops
- **Persist provider balance or raw errors**: categorical availability is sufficient and avoids sensitive telemetry
- **Treat committed cases as an unbiased test**: the vector implementation can inspect them before it freezes
- **Add vector routing to the PageIndex arm**: that changes the architecture being evaluated

## Reuse map

Extend these existing utilities and patterns:

| Need | Existing implementation |
|---|---|
| Untrusted document boundary | `apps/api/src/vectorless_rag/llm.py::UNTRUSTED_DOCUMENT_SYSTEM` and `wrap_untrusted_document` |
| Provider usage normalization | `apps/api/src/vectorless_rag/usage.py` |
| Durable call ownership | `apps/api/src/vectorless_rag/usage.py::usage_context` |
| Object store abstraction | `apps/api/src/vectorless_rag/storage.py::ObjectStore` |
| Process timeout and reaping tests | `apps/api/tests/test_pageindex_adapter.py` |
| Strict API models | `apps/api/src/vectorless_rag/schemas.py::StrictModel` |
| API scope checks | `apps/api/src/vectorless_rag/auth.py` and route dependencies in `api.py` |
| Browser server functions | `apps/web/src/server/functions.ts` |
| Status UI | ASTRYX `Banner` and existing ingestion job status patterns |
| Report aggregation | `apps/api/src/vectorless_rag/reporting.py` |
| Operator CLI | `apps/api/src/vectorless_rag/cli.py` |

## Subtle before and after transitions

### Artifact build

Before:

```text
PDF -> flat schema-v1 JSON -> mutable document/version key -> active pointer
```

After:

```text
PDF -> exact PageIndex pages and nested tree
    -> strict validation and coverage repair
    -> raw object and deterministic page shards
    -> immutable manifest
    -> verified index version
    -> atomic activation and audit event
```

### Ingestion state

Before:

```text
long transaction -> timeout reclamation -> mutable retry
```

After:

```text
queued job -> renewable lease -> external build -> fenced activation
           -> waiting/deferred/requeue on provider interruption
```

### Retrieval

Before:

```text
top-50 lexical candidates -> flat nodes -> overlapping page fetches
```

After:

```text
complete eligible catalog -> deterministic map/reduce -> nested tree expansion
                          -> unioned page evidence with exact truncation locators
```

### Measurement

Before:

```text
raw rows -> destructive interpretation during reporting
```

After:

```text
raw rows + append-only classification events + frozen experiment membership
         -> security-barrier effective views
```

## Migration and rollout sequence

Execute the rollout in this order:

1. Stop ingestion workers and benchmark processes.
2. Back up the `rag` database and record its checksum.
3. Run migration preflight checks for inconsistent active pointers, duplicate active jobs or attempts, and ready-state mismatches.
4. Apply `0004_pageindex_artifact_v2`.
5. Apply `0005_measurement_experiments`.
6. Deploy the API and worker with PageIndex patch `+vr4`.
7. Run provider-free patch, API, web, deployment, and integration tests.
8. Dry-run the seven historical measurement classifications.
9. Apply the exact classification manifest in one serializable admin transaction.
10. Verify raw and filtered historical totals.
11. Freeze the pilot manifest, configuration hash, pricing snapshot, corpus hash, release commit, and schema head.
12. Rebuild only the 25 manifest documents one at a time.
13. Verify every v2 object and run retrieval canaries if budget remains.
14. Keep full-corpus indexing disabled pending review and separate authorization.

## Deliberate exclusions

Do not change these surfaces:

- OCR remains unsupported
- PageIndex Cloud, hosted Model Context Protocol, and enterprise file systems remain out of scope
- No embedding model, vector store, vector reranker, or vector document router enters the PageIndex arm
- No full 537-document build occurs during this implementation
- No application completion cache or partial PageIndex checkpointing is added before the comparison baseline freezes
- No evaluation-management frontend is added
- No historical application or Langfuse row is physically deleted
- SQL, authentication, streaming, and synthesis semantics stay unchanged except for typed provider failures, removal of the masking catalog fallback, and strict citation placement
- Kubernetes log retention remains cluster-managed

## Verification commands

Run these provider-free gates from the repository root:

```bash
make format
make api-check
make web-check
make deploy-check
make pageindex-patch-test
make evaluation-check
make docker-build-api
make docker-build-web
make playwright
```

Run integration tests against the migrated local database:

```bash
TEST_OWNER_DATABASE_URL=postgresql+psycopg://rag_owner:rag-owner-change-me@127.0.0.1:55432/rag \
TEST_DATABASE_URL=postgresql+psycopg://rag_reader:rag-reader-change-me@127.0.0.1:55432/rag \
make test-integration
```

Provider-backed verification follows the credential-safe live fixture procedure in `CLAUDE.md`. It must use one temporary text-rich PDF, must never persist credentials in Playwright artifacts, and must not sync the 537-file corpus.

Completion requires all gates to pass, raw history totals to remain unchanged, classified totals to reconcile, all 25 pilot documents to share one verified v2 recipe, pilot spend to remain at or below $10, and full-corpus indexing to remain disabled.

## Implementation record

Implementation commit `94b43c799fa1f0a947e5411f0275671c6913bf44` is based on `cfbea604c64d51c20ca1cea413b2bfa35be94471` on branch `pageindex-v2-hardening`.

### Implemented revisions and identities

| Item | Value |
| --- | --- |
| Schema revisions | `0004_pageindex_artifact_v2`, `0005_measurement_experiments` |
| PageIndex runtime | `190f8b378be58199ca993566a9214dba72089c54+vr4` |
| PageIndex patch SHA-256 | `2bd3deae1c0432b34281f12745146826b71315c7d08b2d95d385a3c78b6745c0` |
| Provider configuration SHA-256 with repository defaults | `a7001d1aa94effe0915a4f12c9897d36c3d212689b0b4c4feb42002f5cb579b0` |
| V2 pilot runtime configuration SHA-256 with repository defaults | `f9da6f8dd19a933b6fd67008426327433bc78a8eeed6007b8d43ecec8403c20f` |
| Pilot manifest file SHA-256 | `e07009e40813feeb0ef0fd1c5b0ec9d21d6c90585f139d285ce11a1eca8215d9` |
| Pilot corpus snapshot SHA-256 | `316207ff5a90da62cb787feea66f3f940bdf1f6a08933996a39386c7357292fe` |
| Evaluation JSONL SHA-256 | `9682e33721eec592e65f9720d446e13bda49e002b9a24e10cf527df12c831ace` |
| Evaluation corpus file SHA-256 | `b482e1a294dd6c84e8b394acc76ac2657751c2bda2233b7f08cb06a3fcf6cad1` |
| Classification manifest SHA-256 | `8cc22e529521a4e09f6e99628ab846c47413600cba9736fa8f846f274cd3ac20` |

The v2 schema was rendered offline and applied cleanly to disposable PostgreSQL database `rag_v2_validation3_20260725`. Four integration tests passed there, covering grants/guarded SQL, complete-catalog ordering and filters, lease reclaim/concurrent claims, and database-enforced cost reservation/reconciliation. Both disposable validation databases and all four test-only roles created during verification were removed after the tests; the `rag` database was not altered.

### External execution record

No live application data was changed.

- Cleanup/classification event IDs: not created. The exact seven-row manifest is implemented and validates dry-run state, but applying it requires benchmark shutdown, backup plus checksum, replacement validation, and the serializable admin transaction.
- Pilot experiment/run IDs: not created. The manifest validates as exactly 25 PDFs with page-band counts `8/4/4/4/5`; provider-backed execution requires a clean committed release, matching `RELEASE`, schema head, available credit, exact local PDFs, complete prices, and explicit confirmation of the $10 ceiling.
- Pilot spend and quality: no v2 spend or quality result. Historical vr3 spend remains `$0.767451`; it was not rewritten.
- Evaluation run ID: not created. All 120 source records validate, but all 120 review records are intentionally `pending`. Seeding and experiment preparation reject them until a human reviews each case.
- Full-corpus indexing: not run and still disabled.

### Implemented deviations and remaining external gates

The evaluation tooling freezes and validates the complete 4,320-trial interleaved plan and produces deterministic aggregate reports. It does not execute provider-backed trials in this unreviewed state. This is a deliberate fail-closed condition, not a substituted result.

The twelve pilot retrieval canaries are likewise recorded as pending reviewed evaluation cases. Running fabricated or unreviewed canaries would violate the review requirement. After review and pilot indexing, execute them within the same frozen budgeted experiment if remaining reservations permit.

The implementation does not apply historical classifications, run the credentialed live fixture, run the paid pilot, or authorize full-corpus indexing. Those steps affect live data or provider spend and remain operator-controlled.

### Verification results

All results below are from the final working-tree source state. The PageIndex
patch was regenerated as a canonical, ordered diff against the pinned upstream
tree after a clean-clone image build exposed whitespace-sensitive hunks in the
earlier patch file.

| Command or gate | Result |
| --- | --- |
| `make format` | Passed; 66 Python files were already formatted and ESLint fixes produced no further changes. |
| `make api-check` | Passed; Ruff and basedpyright clean, 293 tests passed, 4 skipped, 66% aggregate coverage, and Alembic rendered through `0005`. |
| `make web-check` | Passed; ESLint and TypeScript clean, 29 Vitest tests passed, React Doctor 100/100, production build/smoke passed, and ASTRYX doctor reported no failures. |
| `make deploy-check ENV_FILE=.env.example` | Passed with Helm 3.21.0 from the checksum-verified temporary tool directory; workflow YAML, both Compose configurations, Helm lint, and Helm render passed. |
| Provider-free clean PageIndex patch check | Passed against pinned upstream `190f8b378be58199ca993566a9214dba72089c54`; the patched parser test reported success. |
| `make evaluation-check` | Passed: 120 cases, 48 development and 72 regression, six strata of 20, 40 vocabulary-mismatch cases, 12 exact-phrase cases, and 4,320 deterministic trials. All 120 reviews remain pending. |
| `make pilot-check` | Passed: exactly 25 documents; page bands `8/4/4/4/5`; 16 outline and 9 no-outline PDFs. |
| `make docker-build-api` | Passed from a clean pinned PageIndex clone; image `vectorless-rag-api:test` is `sha256:813421e429214b907df4de5dd60ad78d64f3e87dcd17ad892578040af94dede6`. |
| `make docker-build-web` | Passed; image `vectorless-rag-web:test` is `sha256:7dcc5cd6e6165dd88da79edb0b4e81797caf175e8e60469258ef6eca2c91fe7f`. |
| `make playwright` | Passed against the deterministic mock: 20 tests passed and the credentialed live-stack test was intentionally skipped. Traces, video, screenshots, and storage state remained disabled. |
| Disposable PostgreSQL migration and integration gate | Passed: both migrations applied at schema head and 4 integration tests passed. Validation databases and roles were then removed. |
| `git diff --check` | Passed. Patch files explicitly disable end-of-line whitespace diagnostics because their context must preserve pinned upstream whitespace. |

The credential-safe live fixture, exact seven-event historical classification,
and paid 25-PDF pilot were not executed. Consequently there are no cleanup
event IDs, pilot run IDs, v2 provider spend, or v2 quality results to append.
Those are rollout operations, not missing deterministic verification.
