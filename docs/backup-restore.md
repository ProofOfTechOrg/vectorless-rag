# Backup and restore

Back up PostgreSQL and object storage as one recovery point. PostgreSQL contains catalog,
authorization, audits, jobs and LangGraph checkpoints; MinIO contains canonical PDFs and PageIndex
artifacts. Langfuse additionally needs its PostgreSQL database, ClickHouse and its S3 objects.

For Compose, stop API/worker writes, run `pg_dump` separately for `rag` and `langfuse`, snapshot
the MinIO and ClickHouse volumes, then resume. Encrypt backups and test restores quarterly.

Restore into empty infrastructure in this order: PostgreSQL roles/databases, PostgreSQL dumps,
MinIO objects, ClickHouse data, then start Langfuse, API and workers. Run migrations before serving
traffic. Verify artifact SHA-256 values against `agent.index_versions`; enqueue only mismatches or
missing objects with `force=true`. Never treat Langfuse as the source for security audits.

