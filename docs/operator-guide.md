<!-- Content type: How-to -->

# Operate Vectorless RAG

Use this guide to connect an operator, ingest a bounded PDF set, inspect browser workflows, and verify a disposable live stack.

## Start and connect

Start the complete local stack, verify its health, and mint the least-privilege key needed for the intended work.

Configure `.env`, run `make up`, then `make health`. Mint a least-privilege key with `make bootstrap-key NAME=operator SCOPES=chat,documents:read,admin:ingest`. The raw value is shown once. Open `http://localhost:3001/connect`; API key lifecycle management is deliberately outside the UI.

## Ingestion and pilot gate

Use the Ingestion page for one PDF, or `make upload-document PDF=/path/file.pdf`. Jobs are durable. Use `make logs SERVICE=worker` for worker detail.

The UI makes up to 120 sequential polling attempts, separated by 1.5 seconds. This covers approximately three minutes after request time. The UI then reports a timeout, but the durable job may continue.

Mounted sync scans only direct PDF children of `ARXIV_PDF_HOST_DIR`. `make sync-corpus` deduplicates by content. Force mode reprocesses known files and requires an explicit UI confirmation or `FORCE=true` on the Make target.

With `ALLOW_FULL_CORPUS_INDEX=false`, only ten unknown documents can be registered. Read the [token-cost analysis](token-cost-analysis.md) before using `make pilot-report`: the current report does not receive token telemetry and therefore reports zero provider tokens. Do not enable the full corpus or sync the normal 537-file development directory until job-level cost accounting is fixed and a representative pilot is approved.

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
