# Operator guide

## Ingestion lifecycle

The API stores a PDF by SHA-256 and queues a PostgreSQL job. Workers claim jobs with
`FOR UPDATE SKIP LOCKED`, extract first-page metadata, run PageIndex in a unique temporary
workspace, normalize the tree plus page text, and upload the versioned JSON artifact to MinIO.
Retries reuse completed versions unless `force` is true. Empty/extraction-poor PDFs become
`unsupported`; v1 intentionally has no OCR.

The mounted-folder endpoint accepts only `{"force": boolean}` and always reads direct `.pdf`
children of `ARXIV_PDF_DIR`. It never accepts a server path. Uploads are limited to 200 MB.

### Synchronizing the mounted corpus

The service does not watch the mounted directory for changes. Adding or replacing a PDF does not
start ingestion until an operator calls the synchronization endpoint with an API key containing
the `admin:ingest` scope:

```bash
curl -X POST http://localhost:8000/v1/admin/ingestion/sync \
  -H "X-API-Key: $VECTORLESS_RAG_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"force": false}'
```

Synchronization scans only PDFs directly inside `ARXIV_PDF_DIR`; it does not recurse into
subdirectories. It copies newly discovered PDFs to canonical object storage and queues PostgreSQL
jobs. The ingestion worker then extracts metadata and creates the PageIndex artifact
asynchronously, so a successful sync response means that work was queued, not that indexing has
finished. Use `GET /v1/documents` to inspect document status and
`GET /v1/admin/ingestion/{job_id}` when a job ID is available.

PDFs are identified by content SHA-256 rather than filename:

- `force: false` registers and queues new content, but does not requeue an identical PDF.
- `force: true` requeues an existing document unless it already has a queued or running job.
- Replacing a file with different content registers a new document because its checksum changes.
- Renaming or copying an unchanged PDF does not create another document.
- Removing a PDF from the mounted directory does not delete its database record, canonical PDF,
  or index artifact. Corpus deletion is intentionally not performed by directory synchronization.

Alternatively, `POST /v1/admin/documents` accepts a PDF upload up to 200 MB and queues it
immediately; uploaded files do not require a subsequent directory sync.

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

Create the first Vectorless RAG API key inside the running API container:

```bash
docker compose exec api vectorless-rag bootstrap-key --name operator
```

This invokes the service CLI inside the container, creates an API-key record named `operator`,
and grants its default `chat`, `documents:read`, and `admin:ingest` scopes. The name is an audit
label, not a login or Langfuse user. The command prints the plaintext `vrag_...` key exactly once;
only its prefix and an HMAC-SHA256 hash are stored in PostgreSQL. Save the output in a secret
manager and pass it to API requests through the `X-API-Key` header.

Create a least-privilege key by selecting scopes explicitly:

```bash
docker compose exec api vectorless-rag bootstrap-key \
  --name chat-client \
  --scopes chat,documents:read
```

For non-interactive capture, disable Compose's pseudo-TTY:

```bash
VECTORLESS_RAG_ADMIN_KEY="$(
  docker compose exec -T api vectorless-rag bootstrap-key --name operator
)"
```

`API_KEY_PEPPER` is loaded from `.env` and participates in every stored key hash. Generate it
before creating the first key, for example with `openssl rand -hex 32`, and never change it while
keys remain active. Losing a plaintext key requires creating a replacement; it cannot be recovered
from the database. Rotate by creating a replacement, updating clients, then setting the old
`agent.api_keys.disabled` flag through an audited operator procedure. Running `bootstrap-key`
again creates another key; it does not replace or disable earlier keys.

## Operational checks

- `/health/live` checks the process; `/health/ready` checks PostgreSQL.
- PostgreSQL `sql_audits` and `retrieval_audits` are the authoritative audit records.
- Langfuse is diagnostic. Long passages/results are represented by hashes, sizes and previews.
- A request is cancelled after 120 seconds and SSE disconnects propagate cancellation.
