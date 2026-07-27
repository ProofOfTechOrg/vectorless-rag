<!-- Content type: How-to -->
<!-- Content plan: Goal: back up and restore a consistent Vectorless RAG deployment. Audience: operators. Content: persistent state, credentials, telemetry classification, restoration, and validation. Open questions: provider-specific backup automation. -->

# Back up and restore Vectorless RAG

Use this page to identify persistent state, back it up, restore consistent data and credentials, and validate recovery. Persistent state remains in the Compose volumes `postgres_data`, `clickhouse_data`, `clickhouse_logs`, `redis_data`, `minio_data`, and `pageindex_work`.

The repository move does not rename or remove these volumes. `make down` preserves them.

Back up PostgreSQL with `pg_dump`, MinIO with an S3-compatible replication/copy tool, and Langfuse/ClickHouse according to the deployed topology. Corpus PDFs mounted from `arxiv-pdfs` or another host path are user-owned and must be backed up separately.

Restore database and object-store data as a consistent set, restore the same application credentials and `API_KEY_PEPPER`, apply `make migrate`, then start API/worker before web. Restoring `SESSION_SECRET` preserves unexpired browser sessions; rotating it intentionally logs every browser out. Validate with `make health`, a document detail lookup, and a cited test question.

Before applying historical telemetry classifications, stop benchmark activity, create and checksum a `rag` database backup, save the exact classification manifest and dry-run output, then run:

```bash
docker compose exec -T api vectorless-rag telemetry-classify \
  --manifest telemetry/classifications-v1.json
docker compose exec -T api vectorless-rag telemetry-classify \
  --manifest telemetry/classifications-v1.json --apply --confirm-count 7
docker compose exec -T api vectorless-rag telemetry-classifications
```

The apply command uses `DATABASE_ADMIN_URL`, a serializable transaction, and an advisory lock. It appends classification events; it does not delete attempts, calls, query runs, retrieval audits, SQL audits, Langfuse traces, ClickHouse data, or MinIO data. Preserve the pre-apply backup even after reconciliation succeeds.

Kubernetes PVC and external-service backups follow provider-native procedures. The chart path is `infra/helm/vectorless-rag`; render the exact release values and Secret key inventory alongside backups without storing secret values in Git.
