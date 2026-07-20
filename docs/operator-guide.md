# Operator guide

## Start and connect

Configure `.env`, run `make up`, then `make health`. Mint a least-privilege key with `make bootstrap-key NAME=operator SCOPES=chat,documents:read,admin:ingest`. The raw value is shown once. Open `http://localhost:3001/connect`; API key lifecycle management is deliberately outside the UI.

## Ingestion and pilot gate

Use the Ingestion page for one PDF, or `make upload-document PDF=/path/file.pdf`. Jobs are durable and the UI polls queued/running work through completion, failure, or unsupported status. Use `make logs SERVICE=worker` for worker detail.

Mounted sync scans only direct PDF children of `ARXIV_PDF_HOST_DIR`. `make sync-corpus` deduplicates by content. Force mode reprocesses known files and requires an explicit UI confirmation or `FORCE=true` on the Make target.

With `ALLOW_FULL_CORPUS_INDEX=false`, only ten unknown documents can be registered. Review `make pilot-report` for extraction quality, failures, duration, tokens, retrieval/citation behavior, and projected cost before enabling the full corpus. Do not casually sync the normal 537-file development directory.

## Browser operations

Chat exposes selected route, retrieval/writing status, cancellation, retry, Markdown, page citations, recent threads, and feedback. Documents shows status, topics, diagnostics, and detail landing pages. Settings verifies key name/scopes and API readiness, changes system/light/dark appearance, and clears the cookie on logout.

## Live verification

Use a disposable stack and a dedicated `codex-ui-dev` full-scope key. Pass the key directly in `PLAYWRIGHT_API_KEY` without printing or saving it. Create one temporary text-rich PDF with a unique phrase, log in through `/connect`, upload, await a terminal job, and make at most one unique-phrase chat call after successful ingestion. Verify citation/page range, thread history, feedback, detail, settings, responsive navigation, theme, and logout. Remove the temporary PDF afterward.

For a real mounted-sync check, use an isolated Compose override mounting only that fixture. Let deduplication prevent a second ingestion. Credentialed Playwright runs keep trace, video, screenshot, and storage-state artifacts disabled.
