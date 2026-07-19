# Deployment

## Compose

Compose runs the API, ingestion worker, PostgreSQL, Langfuse web/worker, ClickHouse, Redis and
MinIO. App and Langfuse use separate PostgreSQL databases/roles. The ArXiv directory is mounted
read-only. Replace every default credential before exposing the host.

Compose automatically reads `.env` from the project directory. Start from the supplied template
and restrict access because it contains credentials:

```bash
cp .env.example .env
chmod 600 .env
```

Replace every `change-me` value before exposing the services. The default PDF host directory is
`./arxiv-pdfs`, mounted read-only at `/corpus` in both application containers. Set an absolute or
Compose-project-relative source path in `.env` to use another directory:

```dotenv
ARXIV_PDF_HOST_DIR=/srv/vectorless-rag/pdfs
```

`compose.yaml` deliberately has no environment fallbacks. Every variable in `.env.example` must
remain defined in `.env`; optional values are represented by an empty value. This makes `.env`
the single source of truth and prevents development credentials from being selected silently.

### Local and production requirements

| Fields | Local development | Production |
| --- | --- | --- |
| `ENVIRONMENT`, `RELEASE` | Required; template values are acceptable | Required; use `production` and an immutable release identifier |
| `POSTGRES_PASSWORD`, `RAG_OWNER_PASSWORD`, `RAG_APP_PASSWORD`, `RAG_READER_PASSWORD` | Required; development values are acceptable on an isolated machine | Required; replace each with a different strong secret |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `PAGEINDEX_MODEL` | Required for ingestion and chat; without the API key only non-LLM operations work | Required |
| `DEEPSEEK_INPUT_COST_PER_MILLION_USD`, `DEEPSEEK_OUTPUT_COST_PER_MILLION_USD` | Optional; may be empty | Optional, but required for projected pilot spend |
| `ARXIV_PDF_HOST_DIR`, `PILOT_DOCUMENT_LIMIT`, `ALLOW_FULL_CORPUS_INDEX` | Required; keep the pilot defaults initially | Required; enabling the full corpus still requires pilot approval |
| `API_KEY_PEPPER` | Required before running `bootstrap-key`; a development value works locally | Required; generate a strong secret and keep it stable |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `REDIS_PASSWORD` | Required for the bundled stack; development values are acceptable locally | Required and must be unique strong credentials when bundled services are used |
| `POSTGRES_PORT`, `MINIO_CONSOLE_PORT` | Required; template ports are acceptable | Required; expose according to the deployment network policy |
| `LANGFUSE_DB_PASSWORD`, `LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`, `NEXTAUTH_URL` | Required to run Langfuse; generated secrets are recommended | Required; generate all secrets, use the public HTTPS URL, and keep encryption values stable |
| `LANGFUSE_INIT_ORG_ID`, `LANGFUSE_INIT_PROJECT_ID`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY` | Required for headless project/API-key initialization; project keys may be empty only when configuring the project interactively | Required for this service's tracing; use stable deployment-specific values |
| `LANGFUSE_INIT_ORG_NAME`, `LANGFUSE_INIT_PROJECT_NAME` | Optional descriptions, but must remain defined | Optional descriptions, but must remain defined |
| `LANGFUSE_INIT_USER_EMAIL`, `LANGFUSE_INIT_USER_PASSWORD` | Optional pair; leave empty for interactive signup | Optional pair; configure together only when creating a UI owner headlessly |
| `LANGFUSE_INIT_USER_NAME` | Optional; may be empty | Optional; may be empty |

For Kubernetes, equivalent production values live in the referenced Secret and Helm values rather
than a Compose `.env` file.

Then recreate the two application services:

```bash
docker compose up -d --force-recreate api worker
```

`ARXIV_PDF_DIR` is fixed to the container path `/corpus`; `ARXIV_PDF_HOST_DIR` controls its host
source. After adding PDFs, call `POST /v1/admin/ingestion/sync`; the stack has no automatic
filesystem watcher. See the [operator guide](operator-guide.md#synchronizing-the-mounted-corpus)
for synchronization and deduplication behavior.

### Langfuse initialization

Langfuse headless initialization creates resources only when they do not already exist. The
supplied `.env.example` provides organization and project IDs. To initialize a project with
deterministic API credentials, set both keys in `.env`:

```dotenv
LANGFUSE_INIT_ORG_ID=local-org
LANGFUSE_INIT_ORG_NAME=Local
LANGFUSE_INIT_PROJECT_ID=vectorless-rag
LANGFUSE_INIT_PROJECT_NAME=Vectorless RAG
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=lf_pk_replace_with_random_value
LANGFUSE_INIT_PROJECT_SECRET_KEY=lf_sk_replace_with_random_value
```

The ID fields and both project keys are required to create the project and its API credentials;
organization and project names are optional. The public and secret values are independent random
identifiers, not a mathematical key pair. Generate suitable values before placing them in `.env`:

```bash
printf 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY=lf_pk_%s\n' "$(openssl rand -hex 32)"
printf 'LANGFUSE_INIT_PROJECT_SECRET_KEY=lf_sk_%s\n' "$(openssl rand -hex 32)"
```

To also create the first UI owner without interactive signup, set the initial user's email and
password; its name is optional:

```dotenv
LANGFUSE_INIT_USER_EMAIL=operator@example.com
LANGFUSE_INIT_USER_NAME=Operator
LANGFUSE_INIT_USER_PASSWORD=replace-with-a-long-random-password
```

The initial user depends on `LANGFUSE_INIT_ORG_ID`. Project keys are passed to the RAG API and
worker as `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, enabling tracing automatically. For an
already initialized project, use the credentials shown in Langfuse project settings; changing
`LANGFUSE_INIT_*` values does not rotate existing resources.

Langfuse also requires deployment secrets that are separate from headless resource initialization.
Generate and set these `.env` values rather than using the development defaults:

```bash
printf 'LANGFUSE_NEXTAUTH_SECRET=%s\n' "$(openssl rand -hex 32)"
printf 'LANGFUSE_SALT=%s\n' "$(openssl rand -hex 32)"
printf 'LANGFUSE_ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)"
```

`LANGFUSE_ENCRYPTION_KEY` must be exactly 64 hexadecimal characters. Also set a strong
`LANGFUSE_DB_PASSWORD` and keep all persisted secret values stable. Do not double-quote
`LANGFUSE_INIT_*` values in Compose `.env` files.

After changing initialization values, recreate Langfuse and the application processes:

```bash
docker compose up -d --force-recreate langfuse-web api worker
```

### Existing Compose installations

Treat persisted credentials differently from first-install placeholders. If the stack has already
created Docker volumes:

- Do not change `POSTGRES_PASSWORD`, `RAG_OWNER_PASSWORD`, `RAG_APP_PASSWORD`,
  `RAG_READER_PASSWORD`, or `LANGFUSE_DB_PASSWORD` only in `.env`. PostgreSQL initialization
  scripts run only for a new data directory; rotate existing roles in PostgreSQL first and then
  update `.env` as one coordinated operation.
- Do not change `LANGFUSE_ENCRYPTION_KEY` or `LANGFUSE_SALT` after Langfuse has stored data unless
  following an explicit Langfuse migration procedure. Losing the encryption key can make stored
  encrypted values unreadable.
- Do not change `API_KEY_PEPPER` while Vectorless RAG API keys remain active. Doing so invalidates
  every stored API-key hash; create and distribute replacement keys as part of any rotation.
- If the Langfuse project already exists, copy its existing public and secret credentials from
  Langfuse project settings into `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` and
  `LANGFUSE_INIT_PROJECT_SECRET_KEY`. Initialization variables create missing resources; they do
  not rotate an existing project's credentials.
- Changing `LANGFUSE_NEXTAUTH_SECRET` invalidates existing web sessions, so schedule that rotation
  deliberately.

Do not delete Compose volumes merely to apply new credentials unless loss and restoration of the
persisted databases, artifacts, and indexes is explicitly intended. See the
[backup and restore guide](backup-restore.md) before credential or storage migrations.

## Helm

The chart in `deploy/helm/vectorless-rag` supports bundled or external PostgreSQL, ClickHouse,
Redis and S3. Set each `*.bundled=false` and its external host/endpoint when using managed
infrastructure. `corpus.existingClaim` must be a PVC readable by API and worker pods.

Set `corpus.existingClaim` to the PVC containing the PDFs and, if needed, change
`corpus.mountPath` from its `/corpus` default. The chart sets `ARXIV_PDF_DIR` to that mount path.
Updating files in the PVC still requires an explicit ingestion sync; Kubernetes deployments do
not run a directory watcher either.

Create `existingSecret` with these keys:

`database-url`, `database-admin-url`, `sql-database-url`, `deepseek-api-key`, `api-key-pepper`,
`s3-access-key`, `s3-secret-key`, `postgres-password`, `rag-owner-password`, `rag-app-password`,
`rag-reader-password`, `langfuse-db-password`, `langfuse-database-url`, `langfuse-public-key`,
`langfuse-secret-key`, `langfuse-nextauth-secret`, `langfuse-salt`, `langfuse-encryption-key`,
`clickhouse-password`, and `redis-password`.

Use PostgreSQL URLs with `postgresql+psycopg://` for application keys and `postgresql://` for
Langfuse. Set an ingress TLS secret and resource/storage classes appropriate for the cluster.
Bundled services are single-replica and intended for modest installations, not HA.
