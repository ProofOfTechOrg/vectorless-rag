<!-- Content type: How-to -->
<!-- Content plan: Goal: operate bounded ingestion, inspect browser workflows, and run credential-safe live verification. Audience: local and release operators. Content: connection, ingestion, pilot safety, artifacts, browser operations, and disposable live tests. Open questions: none. -->

# Operate Vectorless RAG

Use this guide to connect an operator, ingest a bounded PDF set, inspect browser workflows, and verify a disposable live stack.

## Start and connect

Start the complete local stack, verify its health, and mint the least-privilege key needed for the intended work.

Configure `.env`, run `make up`, then `make health`. Mint a least-privilege key with `make bootstrap-key NAME=operator SCOPES=chat,documents:read,admin:ingest`. The raw value is shown once. Open `http://localhost:3001/connect`; API key lifecycle management is deliberately outside the UI.

## Ingestion and pilot gate

Use the Ingestion page for one PDF, or `make upload-document PDF=/path/file.pdf`. Jobs are durable. Use `make logs SERVICE=worker` for worker detail.

The UI polls every 1.5 seconds until the job reaches a terminal state. Transient status failures switch to five-second polling and do not discard the durable job. A provider wait or deferred state remains visible and continues polling. A fresh successful provider check can resume a waiting job after the provider retry cooldown. Cached checks and fresh checks made during that cooldown cannot resume it.

Mounted sync scans only direct Portable Document Format (PDF) children of `ARXIV_PDF_HOST_DIR`. `make sync-corpus` deduplicates by content. The API supports three modes:

- `ensure_current` reuses a compatible index or stored metadata
- `rebuild_index` rebuilds the index from compatible stored metadata
- `reextract_all` extracts fresh metadata and rebuilds the index

Selecting either rebuild mode bypasses content deduplication. Deprecated `force=true` maps to `rebuild_index` for one release.

With `ALLOW_FULL_CORPUS_INDEX=false`, only 25 documents can be registered. Capacity is reported dynamically. Durable call accounting includes metadata extraction and PageIndex calls. It records measured usage when the provider returns it and retains a conservative reservation when it does not. Exhausted transient provider retries consume retry budget. Credit and provider-availability waits do not consume retry budget.

Run the tracked pilot only after committing the release and setting `RELEASE` to that exact SHA:

```bash
make restart-app
make pilot-check
make pilot-run
make artifact-verify
make pilot-report
```

`make pilot-run` requires a clean worktree, a running API image whose `RELEASE` equals the current commit, schema head `0007_model_call_experiment`, an available provider, all 25 exact PDFs, a complete price snapshot, and no active ingestion jobs. The Make target reserves `apps/api/pilots/run/` before it invokes the API container. An existing directory stops the command before provider work. A failed run leaves the directory reserved; inspect it before authorizing another run.

The pilot queues one manifest document at a time with `reextract_all`. PostgreSQL rejects any logical call whose three-attempt worst-case reservation would exceed the $10 experiment cap. The worker then defers the job without consuming retry budget. Do not enable full-corpus indexing or run ordinary 537-file sync until the pilot is accepted and separately authorized.

Use `vectorless-rag artifact-rollback --document-id … --version-id …` to activate a verified prior version. The command verifies the manifest and every referenced object before it changes the active pointer. Rollback and applied garbage collection use the same database lock, so garbage collection cannot delete a version while it is being activated. `make artifact-gc` is dry-run only. Applying cleanup requires the exact dry-run count:

```bash
docker compose exec -T api vectorless-rag artifact-gc --apply --confirm-count COUNT
```

Garbage collection preserves active versions, the newest two verified versions per document, frozen-experiment versions, shared objects, and orphans newer than 24 hours. It only considers `artifacts/v2/`; it never reads or modifies source PDFs.

## Browser operations

Use the browser console to exercise scoped chat, document, ingestion, and settings operations through the browser-facing frontend (BFF).

Chat exposes selected route, retrieval/writing status, cancellation, retry, Markdown, page citations, recent threads, and feedback. Documents shows status, topics, diagnostics, and detail landing pages. Settings verifies key name/scopes and API readiness, changes system/light/dark appearance, and clears the cookie on logout.

## Verify a disposable live stack

This procedure isolates live verification from the normal Compose project and deletes only the disposable project when finished.

1. Stop the normal stack while preserving its named volumes:

   ```bash
   make down
   ```

2. Start a separately named project from the same `.env` file:

   ```bash
   docker compose --project-name vectorless-rag-live --env-file .env up --build -d
   ```

3. Mint a dedicated full-scope key and capture it directly into the current shell’s process environment. The command does not print or save the key:

   ```bash
   export PLAYWRIGHT_API_KEY="$(
     docker compose --project-name vectorless-rag-live --env-file .env \
       exec -T api vectorless-rag bootstrap-key \
       --name codex-ui-dev \
       --scopes chat,documents:read,admin:ingest
   )"
   ```

4. Run the live browser test against the disposable frontend, then remove the key from the shell:

   ```bash
   make playwright-live PLAYWRIGHT_BASE_URL=http://localhost:3001
   unset PLAYWRIGHT_API_KEY
   ```

   The test creates one text-rich PDF in a temporary directory, connects through `/connect`, uploads the PDF from `/ingestion`, and removes that exact directory in a `finally` block. It waits for a terminal ingestion status and makes one unique-phrase chat request only after successful ingestion. It also verifies citation pages, thread history, feedback, document detail, identity, scopes, theme, responsive navigation, and logout.

5. Delete only the disposable project and its volumes by repeating the explicit project name:

   ```bash
   docker compose --project-name vectorless-rag-live --env-file .env down -v
   ```

For a real mounted-sync check, use an isolated Compose override mounting only that fixture. Let deduplication prevent a second ingestion. Credentialed Playwright runs keep trace, video, screenshot, and storage-state artifacts disabled.
