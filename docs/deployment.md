# Deployment

## Required configuration

The API retains its database, DeepSeek, PageIndex, object-store, Langfuse, pepper, and pilot settings. The web BFF adds:

- `VECTORLESS_API_URL`: private FastAPI base URL, such as `http://api:8000`.
- `APP_ORIGIN`: exact public browser origin, including scheme and non-default port.
- `SESSION_SECRET`: exactly 32 random bytes, encoded as 64 hex characters or base64.
- `FRONTEND_PORT`: local Compose host port; default 3001.

Generate a development secret with `make secret-session`. Never use `VITE_` for these values.

## Compose

`compose.yaml` builds API/worker from `apps/api` and web from `apps/web/Dockerfile`. It mounts PostgreSQL initialization from `infra/docker/postgres-init` and preserves the existing `postgres_data`, `clickhouse_data`, `clickhouse_logs`, `redis_data`, `minio_data`, and `pageindex_work` volumes.

```bash
make compose-check ENV_FILE=.env.example
make up
make restart-app
make restart-web
make health
```

Public local endpoints are web 3001, Langfuse 3000, and FastAPI 8000. The BFF reaches FastAPI over the private Compose network.

## Images

```bash
make docker-build-api
make docker-build-web
```

The web runtime uses Node 22 as its non-root `node` user and exposes `/healthz`. The API remains non-root UID 10001.

## Helm

The chart is `infra/helm/vectorless-rag`. The existing `ingress` continues to target the API. `frontendIngress` is independent and targets the web service. Set `appOrigin` to that frontend's exact HTTPS origin.

The existing Secret must also contain `session-secret` alongside the documented database, model, object-store, Langfuse, and pepper keys. The frontend Deployment receives only `SESSION_SECRET`, the internal API service URL, and `APP_ORIGIN`; API keys are supplied by users at login and are not Kubernetes secrets.

```bash
make helm-check
helm upgrade --install vectorless-rag infra/helm/vectorless-rag \
  --namespace vectorless-rag --create-namespace \
  --set frontendIngress.enabled=true \
  --set frontendIngress.host=rag.example.com \
  --set appOrigin=https://rag.example.com
```

Bundle choices for PostgreSQL, ClickHouse, Redis, and S3 remain intended for modest installations. Use managed/high-availability services and external secret management for production.
