<!-- Content type: How-to -->
<!-- Content plan: Goal: configure and validate Compose or Helm deployments. Audience: operators and platform engineers. Content: runtime configuration, services, images, chart values, secrets, and validation. Open questions: high-availability reference architecture. -->

# Deploy Vectorless RAG

Use this page to configure the frontend, validate Compose or Helm manifests, build images, and supply the complete Kubernetes Secret inventory.

## Required configuration

Configure the browser-facing frontend (BFF) with server-only runtime variables. The API retains its database, DeepSeek, PageIndex, object-store, Langfuse, pepper, and pilot settings.

The BFF adds:

- `VECTORLESS_API_URL`: private FastAPI base URL, such as `http://api:8000`.
- `APP_ORIGIN`: exact public browser origin, including scheme and non-default port.
- `SESSION_SECRET`: exactly 32 random bytes, encoded as 64 hex characters or base64.
- `FRONTEND_PORT`: optional local Compose host port; Compose defaults it to 3001 when unset.

Generate a development secret with `make secret-session`. Never use `VITE_` for these values.

## Compose

Use Compose for a local full-stack deployment with persistent named volumes. Use the development override when you need source reload inside containers.

`compose.yaml` defines API and worker images from `apps/api` and the web image from `apps/web/Dockerfile`. It mounts PostgreSQL initialization from `infra/docker/postgres-init` and preserves the existing `postgres_data`, `clickhouse_data`, `clickhouse_logs`, `redis_data`, `minio_data`, and `pageindex_work` volumes. `make up` starts these services in the background without forcing image rebuilds.

Every Compose service uses Docker's `local` log driver with a 20 MB maximum and five files. Kubernetes log retention remains cluster-managed.

```bash
make compose-check ENV_FILE=.env.example
make up
make rebuild
make restart-app
make restart-web
make health
```

`make up` reuses existing images. `make rebuild` rebuilds every local application image and force-recreates the complete detached stack. `make restart-app` and `make restart-web` perform the same rebuild and recreation for their named application services. None of these commands removes named volumes.

`compose.dev.yaml` adds source mounts, Uvicorn and worker reload, Vite hot module replacement, and web-image rebuild watches. Docker Compose 2.22 or later is required. Run it through the Make targets:

```bash
make dev
make dev-health
```

`make dev` is the canonical active-development command. It builds the current images and runs the stack in the foreground. Web source edits update through Vite hot module replacement. Changes to `apps/web/package.json`, the root `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`, or `apps/web/Dockerfile` automatically rebuild and recreate the web service. `make dev-build` remains an optional image prebuild or troubleshooting command. Changes to `.env`, `compose.yaml`, or `compose.dev.yaml` require stopping and rerunning `make dev`. `make compose-check` validates both Compose configurations.

Public local endpoints are web 3001 and Langfuse 3000. Compose publishes FastAPI only on host loopback at `127.0.0.1:8000`, so other hosts cannot connect to it directly.

The BFF reaches FastAPI privately as `http://api:8000` over the Compose network.

### PageIndex v2 settings

Keep these values aligned across `.env`, Compose, and Helm:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `PAGEINDEX_ASYNC_CONCURRENCY` | 8 | Maximum concurrent PageIndex provider calls |
| `PAGEINDEX_MAX_OUTPUT_TOKENS` | 8,192 | Explicit output ceiling used by calls and reservations |
| `QUERY_NODE_SELECTION_MAX_OUTPUT_TOKENS` | 4,096 | Output ceiling for non-thinking query node selection |
| `PAGEINDEX_CALL_TIMEOUT_SECONDS` | 120 | Per-call timeout |
| `INGESTION_LEASE_SECONDS` | 120 | Renewable job lease |
| `INGESTION_HEARTBEAT_SECONDS` | 30 | Lease renewal interval |
| `CATALOG_BATCH_TOKEN_LIMIT` | 24,000 | Complete-catalog map batch size |
| `CATALOG_ROUTING_CONCURRENCY` | 4 | Process-wide concurrent catalog-routing provider calls |
| `DOCUMENT_LIMIT` | 8 | Maximum selected documents |
| `EVIDENCE_TOKEN_LIMIT` | 40,000 | Public evidence ceiling |
| `REQUEST_DEADLINE_SECONDS` | 180 | Complete chat request deadline |
| `PILOT_DOCUMENT_LIMIT` | 25 | Registration safety gate |
| `PILOT_COST_LIMIT_USD` | 10 | Frozen-pilot cost ceiling |

Leave `ALLOW_FULL_CORPUS_INDEX=false` until the manifest-driven pilot is accepted and separately authorized.

## Images

Build the API and frontend images independently when you do not need the complete `make docker-build` target.

```bash
make docker-build-api
make docker-build-web
```

The web runtime uses Node 22 as its non-root `node` user and exposes `/healthz`. Its container healthcheck also fetches the SSR `/connect` document and requires every referenced same-origin stylesheet and module script to return the expected content type. Run the same check against a local production build with `make web-production-smoke`. The API remains non-root UID 10001.

## Helm

Use Helm 4.2.3 to validate or deploy the chart at `infra/helm/vectorless-rag`. This is the version pinned by continuous integration. Helm is not required for general frontend development.

The existing `ingress` targets the API. `frontendIngress` is independent and targets the web service. Set `appOrigin` to the frontend’s exact HTTPS origin.

### Frontend values

These values are the authoritative frontend deployment surface:

| Value | Purpose |
| --- | --- |
| `webImage.repository` | Frontend image repository |
| `webImage.tag` | Frontend image tag |
| `webImage.pullPolicy` | Kubernetes image pull policy |
| `frontendReplicaCount` | Number of frontend Deployment replicas |
| `frontendService.port` | Frontend Service port; the container target remains 3001 |
| `appOrigin` | Exact public browser origin used for mutation origin checks |
| `frontendIngress.enabled` | Creates the independent frontend Ingress when true |
| `frontendIngress.className` | Optional frontend Ingress class |
| `frontendIngress.host` | Frontend Ingress hostname |
| `frontendIngress.annotations` | Frontend Ingress annotations map |
| `frontendIngress.tls` | Frontend Ingress TLS entries |
| `resources.web` | Frontend container CPU and memory requests and limits |

### Frontend environment

The web Deployment receives exactly six runtime variables:

- `NODE_ENV`
- `HOST`
- `PORT`
- `VECTORLESS_API_URL`
- `APP_ORIGIN`
- `SESSION_SECRET`

API keys are supplied by operators at login. They are not stored as Kubernetes Secret values.

### Secret inventory

Create the Secret named by `existingSecret` with every key in this inventory. `infra/helm/vectorless-rag/templates/NOTES.txt` points operators to this list.

<!-- helm-secret-inventory:start -->
- `api-key-pepper`
- `clickhouse-password`
- `database-admin-url`
- `database-url`
- `deepseek-api-key`
- `langfuse-database-url`
- `langfuse-db-password`
- `langfuse-encryption-key`
- `langfuse-nextauth-secret`
- `langfuse-public-key`
- `langfuse-salt`
- `langfuse-secret-key`
- `postgres-password`
- `rag-app-password`
- `rag-owner-password`
- `rag-reader-password`
- `redis-password`
- `s3-access-key`
- `s3-secret-key`
- `session-secret`
- `sql-database-url`
<!-- helm-secret-inventory:end -->

### Validate and install the chart

Lint and render the chart before installation:

```bash
make helm-check
helm upgrade --install vectorless-rag infra/helm/vectorless-rag \
  --namespace vectorless-rag --create-namespace \
  --set frontendIngress.enabled=true \
  --set frontendIngress.host=rag.example.com \
  --set appOrigin=https://rag.example.com
```

Bundle choices for PostgreSQL, ClickHouse, Redis, and S3 remain intended for modest installations. Use managed/high-availability services and external secret management for production.
