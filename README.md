# Vectorless ArXiv RAG

An API-only, self-hosted RAG service for the 537 PDFs in `arxiv-pdfs/`. PostgreSQL narrows
the corpus, DeepSeek reranks documents, and the open-source PageIndex tree is traversed without
embeddings or a vector database. The only hosted runtime dependency is DeepSeek.

## Quick start

1. Copy `.env.example` to `.env` and replace every `change-me` value. Set `DEEPSEEK_API_KEY`
   and the Langfuse initialization values described in the deployment guide.
2. Start the stack with `docker compose up --build -d`.
3. Create the first key:
   `docker compose exec api vectorless-rag bootstrap-key --name operator`.
4. Queue the pilot with `POST /v1/admin/ingestion/sync` and body `{"force": false}`.
5. Inspect `vectorless-rag pilot-report`. The first sync cannot queue more than ten PDFs.

Directory changes are not watched automatically. After adding PDFs to the mounted corpus, call
the sync endpoint again. See the [operator guide](docs/operator-guide.md#synchronizing-the-mounted-corpus)
for an authenticated example, deduplication behavior, and deletion semantics, and the
[deployment guide](docs/deployment.md#compose) for changing the host directory or Kubernetes PVC.

OpenAPI is served at `http://localhost:8000/docs`; Langfuse is at `http://localhost:3000`.
Do not set `ALLOW_FULL_CORPUS_INDEX=true` until the ten-paper quality/cost report has explicit
approval. The remaining 527 files are never queued by default.

## Development

```bash
uv sync --frozen
uv run ruff check .
uv run mypy src
uv run pytest
docker compose config --quiet
helm lint deploy/helm/vectorless-rag
```

PageIndex is pinned to commit `190f8b378be58199ca993566a9214dba72089c54`; its runtime
dependencies are locked in `uv.lock`, and the container installs that exact source revision.

See [operator guide](docs/operator-guide.md), [deployment guide](docs/deployment.md),
[security model](docs/security.md), and [backup/restore](docs/backup-restore.md).
