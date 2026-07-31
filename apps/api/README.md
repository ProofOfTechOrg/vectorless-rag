<!-- Content type: Reference -->
<!-- Content plan: Goal: orient API package contributors without duplicating repository-wide operations. Audience: Python contributors. Content: package scope, command boundaries, provider-use boundary, and authoritative repository links. Open questions: none. -->

# Vectorless RAG API

This package owns the FastAPI service, durable ingestion worker, PageIndex integration, retrieval graph, guarded SQL, provider accounting, Alembic migrations, evaluation tooling, and Python tests. The root `Makefile` is the authoritative command surface.

From the repository root:

```bash
make api-install
make api-test
make api-check
TEST_POSTGRES_PORT=55433 make test-integration-stack
```

Use `uv --project apps/api …` for direct package commands from the repository root, or `uv …` from this directory. Unit, type, migration-render, and isolated PostgreSQL tests are provider-free. Live ingestion, chat, pilots, and evaluation execution call DeepSeek and require separate cost authorization.

Read the [project overview](../../README.md), [command reference](../../docs/commands.md), [test tiers](../../docs/testing.md), and [evaluation workflow](../../docs/evaluation.md) before changing cross-package behavior.
