# Operator guide

## Ingestion lifecycle

The API stores a PDF by SHA-256 and queues a PostgreSQL job. Workers claim jobs with
`FOR UPDATE SKIP LOCKED`, extract first-page metadata, run PageIndex in a unique temporary
workspace, normalize the tree plus page text, and upload the versioned JSON artifact to MinIO.
Retries reuse completed versions unless `force` is true. Empty/extraction-poor PDFs become
`unsupported`; v1 intentionally has no OCR.

The mounted-folder endpoint accepts only `{"force": boolean}` and always reads direct `.pdf`
children of `ARXIV_PDF_DIR`. It never accepts a server path. Uploads are limited to 200 MB.

## Pilot gate

The default `PILOT_DOCUMENT_LIMIT=10` and `ALLOW_FULL_CORPUS_INDEX=false` enforce the first
rollout. Run `vectorless-rag pilot-report` after all ten jobs reach terminal status. Review:

- extraction quality and unsupported diagnostics;
- retrieval/citation evaluation scores;
- elapsed time and provider token totals;
- projected spend for the remaining 527 PDFs.

Set the current hosted-provider rates in `DEEPSEEK_INPUT_COST_PER_MILLION_USD` and
`DEEPSEEK_OUTPUT_COST_PER_MILLION_USD`; the report leaves projected spend unset when rates are
not configured instead of silently applying stale pricing.

After explicit approval, set `ALLOW_FULL_CORPUS_INDEX=true`, restart API and workers, and call
sync again. Jobs remain resumable and failures are quarantined as `failed`/`unsupported`.

## API key operations

`vectorless-rag bootstrap-key --name NAME --scopes chat,documents:read,admin:ingest` prints a
key once. Only an HMAC-SHA256 hash is stored. Rotate by creating a replacement, updating clients,
then setting the old `agent.api_keys.disabled` flag through an audited operator procedure.

## Operational checks

- `/health/live` checks the process; `/health/ready` checks PostgreSQL.
- PostgreSQL `sql_audits` and `retrieval_audits` are the authoritative audit records.
- Langfuse is diagnostic. Long passages/results are represented by hashes, sizes and previews.
- A request is cancelled after 120 seconds and SSE disconnects propagate cancellation.
