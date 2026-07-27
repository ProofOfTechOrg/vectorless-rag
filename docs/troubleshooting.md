<!-- Content type: Troubleshooting -->
<!-- Content plan: Goal: diagnose common local startup, credential, provider, document, and health failures without losing data. Audience: local operators and contributors. Content: symptoms, checks, fixes, and reset boundaries. Open questions: none. -->

# Troubleshoot the local stack

Use the symptom headings below to diagnose local failures. Preserve `.env`, `arxiv-pdfs`, and named volumes until you identify the affected boundary.

## A host port is already allocated

Docker reports an address or port allocation error when another process uses a published port.

Check Compose status and listening processes:

```bash
docker compose ps
ss -ltn
```

The default host ports are console `3001`, Langfuse `3000`, FastAPI `8000`, PostgreSQL `5432`, MinIO console `9001`, ClickHouse HTTP `8123`, and ClickHouse native `9000`.

Change the matching value in `.env` before startup. Use `TEST_POSTGRES_PORT=55433 make test-integration-stack` when only the disposable integration port is occupied.

## Session or encryption secrets are rejected

The web service rejects `SESSION_SECRET` unless it encodes exactly 32 random bytes as 64 hexadecimal characters. Langfuse applies the same 64-character format to `LANGFUSE_ENCRYPTION_KEY`.

Create a fresh local environment only when `.env` does not exist:

```bash
make init
```

If `.env` already contains application data credentials, edit only the invalid value. Changing `SESSION_SECRET` logs out browser sessions. Changing `API_KEY_PEPPER` invalidates existing Vectorless RAG API keys.

## PostgreSQL rejects credentials after an environment change

The PostgreSQL image creates roles only when it initializes an empty volume. Changing database passwords in `.env` does not update roles in an existing volume.

Restore the previous values or rotate passwords inside PostgreSQL. Do not delete the normal volume merely to align a changed `.env`.

For disposable development data only, inspect the exact Compose project before resetting it:

```bash
docker compose ps
docker compose down
```

Adding `--volumes` permanently deletes that project’s database and service data. Never run it against an uncertain project name.

## Ingestion waits for the provider

The console can report unavailable credit, rate limiting, authentication failure, or a retry cooldown. Check **Settings** for sanitized provider state and confirm:

- `DEEPSEEK_API_KEY` belongs to the intended account
- The account has available credit
- `deepseek-v4-pro` remains listed in the [official DeepSeek model and pricing reference](https://api-docs.deepseek.com/quick_start/pricing/)
- The configured retry cooldown has elapsed

Restarting healthy services does not bypass provider cooldown or account state.

## A PDF is unsupported or ingestion fails

Version 0.2.0 supports born-digital PDFs. It does not perform optical character recognition (OCR).

Open the PDF locally and confirm that you can select its text. Use one small file for diagnosis. Inspect sanitized worker logs without copying document text or provider response bodies:

```bash
make logs SERVICE=worker
```

The mounted-corpus sync scans direct PDF children only. It does not recurse into directories.

## Health checks do not pass

Inspect service state and bounded logs:

```bash
make ps
make logs SERVICE=api TAIL=200
make logs SERVICE=web TAIL=200
make health
```

PostgreSQL and MinIO must become healthy before the API starts. The web service then waits for API readiness. Langfuse also depends on PostgreSQL, ClickHouse, Redis, and MinIO.

Run `make deploy-check ENV_FILE=.env.example` when Compose configuration fails before container startup.

## Reset only disposable state

These commands preserve named volumes:

```bash
make stop
make down
```

`docker compose down -v` deletes named volumes for the selected project. Use it only for a confirmed disposable project such as the isolated integration or release-test stack.

Never move, modify, ingest, or delete `arxiv-pdfs` while troubleshooting project infrastructure. That directory is user-owned and outside service cleanup.
