# Security model

The service is single-tenant and uses three API scopes: `chat`, `documents:read`, and
`admin:ingest`. Keys contain 256 bits of random material, are hashed with a server-side pepper,
and are never logged or traced.

PDF text is untrusted data. Metadata extraction, node selection, sufficiency checks, and answer
synthesis delimit it explicitly and instruct the model to ignore embedded directions. PageIndex
is pinned to its prompt-injection-hardened OSS revision. This is defense in depth, not a reason to
accept citations that do not resolve to an indexed document and page range.

Text-to-SQL uses a separate `rag_reader` pool. SQLGlot enforces a single comment-free SELECT/CTE
over `agent.paper_catalog_v1` and `agent.ingestion_summary_v1`; mutation, DDL, locking, dangerous
functions, unapproved relations, nonliteral limits, and stacked statements are rejected. Execution
is read-only with statement/lock timeouts, `EXPLAIN` cost and payload ceilings.

Use a secrets manager in Kubernetes. Restrict network access to API ingress and Langfuse UI;
PostgreSQL, ClickHouse, Redis, and MinIO should remain cluster-private. Rotate all example secrets.

