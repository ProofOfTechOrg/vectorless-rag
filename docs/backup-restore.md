# Backup and restore

Persistent state remains in the Compose volumes `postgres_data`, `clickhouse_data`, `clickhouse_logs`, `redis_data`, `minio_data`, and `pageindex_work`. The repository move does not rename or remove them. `make down` preserves volumes.

Back up PostgreSQL with `pg_dump`, MinIO with an S3-compatible replication/copy tool, and Langfuse/ClickHouse according to the deployed topology. Corpus PDFs mounted from `arxiv-pdfs` or another host path are user-owned and must be backed up separately.

Restore database and object-store data as a consistent set, restore the same application credentials and `API_KEY_PEPPER`, apply `make migrate`, then start API/worker before web. Restoring `SESSION_SECRET` preserves unexpired browser sessions; rotating it intentionally logs every browser out. Validate with `make health`, a document detail lookup, and a cited test question.

Kubernetes PVC and external-service backups follow provider-native procedures. The chart path is `infra/helm/vectorless-rag`; render the exact release values and Secret key inventory alongside backups without storing secret values in Git.
