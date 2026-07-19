# Deployment

## Compose

Compose runs the API, ingestion worker, PostgreSQL, Langfuse web/worker, ClickHouse, Redis and
MinIO. App and Langfuse use separate PostgreSQL databases/roles. The ArXiv directory is mounted
read-only. Replace every default credential before exposing the host.

## Helm

The chart in `deploy/helm/vectorless-rag` supports bundled or external PostgreSQL, ClickHouse,
Redis and S3. Set each `*.bundled=false` and its external host/endpoint when using managed
infrastructure. `corpus.existingClaim` must be a PVC readable by API and worker pods.

Create `existingSecret` with these keys:

`database-url`, `database-admin-url`, `sql-database-url`, `deepseek-api-key`, `api-key-pepper`,
`s3-access-key`, `s3-secret-key`, `postgres-password`, `rag-owner-password`, `rag-app-password`,
`rag-reader-password`, `langfuse-db-password`, `langfuse-database-url`, `langfuse-public-key`,
`langfuse-secret-key`, `langfuse-nextauth-secret`, `langfuse-salt`, `langfuse-encryption-key`,
`clickhouse-password`, and `redis-password`.

Use PostgreSQL URLs with `postgresql+psycopg://` for application keys and `postgresql://` for
Langfuse. Set an ingress TLS secret and resource/storage classes appropriate for the cluster.
Bundled services are single-replica and intended for modest installations, not HA.

