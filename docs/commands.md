# Command reference

The repository Makefile is the supported interface for development, CI, deployment validation,
Compose lifecycle operations, and routine operator tasks. Run `make help` for the same target list.
Direct `uv`, Ruff, mypy, pytest, Docker, Helm, Alembic, and service-CLI commands are implementation
details behind these targets.

Local prerequisites are GNU Make, `uv`, Docker with Compose v2, Helm 3, curl, and OpenSSL. GitHub
Actions installs pinned `uv` and Helm toolchains before invoking Make. The Makefile keeps the `uv`
cache inside the ignored `.uv-cache/` workspace directory so validation does not depend on a
writable global user cache.

## Setup and validation

| Command | Purpose |
| --- | --- |
| `make env` | Create `.env` from `.env.example` only when absent and set mode `0600` |
| `make install` | Install the frozen Python development environment |
| `make format` | Format Python and apply safe Ruff fixes |
| `make lint` | Check Ruff lint and formatting rules |
| `make typecheck` | Run strict mypy checks |
| `make test` | Run all available tests with coverage; unconfigured integration tests skip |
| `make test-unit` | Run tests that require no external infrastructure |
| `make test-integration` | Run PostgreSQL integration tests using exported test database URLs |
| `make workflow-check` | Parse the GitHub Actions workflow YAML |
| `make migration-check` | Render the Alembic migration offline |
| `make compose-check` | Validate Compose using `ENV_FILE=.env` by default |
| `make helm-lint` | Lint the Helm chart |
| `make helm-template` | Render the Helm chart |
| `make helm-check` | Lint and render the Helm chart |
| `make docker-build` | Build the production image as `vectorless-rag:test` |
| `make check` | Run lint, typecheck, tests, migration, Compose, and Helm checks |
| `make ci` | Run the complete GitHub CI-equivalent suite using `.env.example` |

GitHub Actions invokes the same `make install`, `make lint`, `make typecheck`, `make test`,
`make workflow-check`, `make migration-check`, `make compose-check`, `make helm-check`, and
`make docker-build` targets. There is no separate CI-only implementation.

PostgreSQL integration tests require both URLs:

```bash
export TEST_OWNER_DATABASE_URL=postgresql+psycopg://rag_owner:PASSWORD@127.0.0.1:PORT/rag
export TEST_DATABASE_URL=postgresql+psycopg://rag_reader:PASSWORD@127.0.0.1:PORT/rag
make test-integration
```

## Compose lifecycle

| Command | Purpose |
| --- | --- |
| `make up` | Build and start the complete stack |
| `make down` | Remove containers and networks while preserving named volumes |
| `make stop` | Stop containers without removing them |
| `make restart` | Recreate the complete stack without removing volumes |
| `make restart-app` | Recreate API and ingestion worker only |
| `make restart-langfuse` | Recreate Langfuse web and worker only |
| `make restart-observability` | Reload Langfuse credentials in Langfuse, API, and worker |
| `make ps` | Show service state |
| `make logs` | Follow all logs; use `SERVICE=api` and/or `TAIL=500` to narrow output |
| `make health` | Check API and Langfuse health endpoints |

None of the lifecycle targets removes named volumes. Volume deletion is deliberately not exposed
as a convenience target.

## Operator commands

| Command | Purpose |
| --- | --- |
| `make migrate` | Apply application migrations in the API container |
| `make bootstrap-key NAME=operator` | Create a Vectorless RAG API key |
| `make sync-corpus` | Queue mounted PDFs with `force=false` |
| `make sync-corpus FORCE=true` | Requeue eligible existing documents and queue new PDFs |
| `make upload-document PDF=/path/paper.pdf` | Upload and queue one PDF |
| `make pilot-report` | Print the ten-document pilot report |
| `make evaluation-build` | Rebuild the deterministic 85-case evaluation dataset |
| `make evaluation-seed` | Upload the dataset to Langfuse |

`sync-corpus` and `upload-document` require an exported key so it is not placed in shell history as
a Make argument:

```bash
export VECTORLESS_RAG_ADMIN_KEY=vrag_...
make sync-corpus
```

Override key scopes with `SCOPES`, evaluation input with `DATASET`, and the Langfuse dataset name
with `DATASET_NAME`. Run `make help` for defaults.

## Secret generation

`make secret-api-pepper` and `make secrets-langfuse` print generated `.env` assignments. They do
not modify `.env`; review and paste their output into the intended deployment secret store. The
generated initial-user password is hexadecimal so Compose cannot reinterpret `$` characters.
