<!-- Content type: How-to -->

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

Use Compose for a local full-stack deployment with persistent named volumes.

`compose.yaml` builds API/worker from `apps/api` and web from `apps/web/Dockerfile`. It mounts PostgreSQL initialization from `infra/docker/postgres-init` and preserves the existing `postgres_data`, `clickhouse_data`, `clickhouse_logs`, `redis_data`, `minio_data`, and `pageindex_work` volumes.

```bash
make compose-check ENV_FILE=.env.example
make up
make restart-app
make restart-web
make health
```

Public local endpoints are web 3001 and Langfuse 3000. Compose publishes FastAPI only on host loopback at `127.0.0.1:8000`, so other hosts cannot connect to it directly.

The BFF reaches FastAPI privately as `http://api:8000` over the Compose network.

## Images

Build the API and frontend images independently when you do not need the complete `make docker-build` target.

```bash
make docker-build-api
make docker-build-web
```

The web runtime uses Node 22 as its non-root `node` user and exposes `/healthz`. The API remains non-root UID 10001.

## Helm

Use Helm 3 to validate or deploy the chart at `infra/helm/vectorless-rag`. Helm is not required for general frontend development.

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
