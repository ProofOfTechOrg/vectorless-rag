FROM ghcr.io/astral-sh/uv:0.8.3 AS uv
FROM python:3.12.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends git libmagic1 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
RUN uv sync --frozen --no-dev && sh /app/scripts/install-pageindex.sh /opt/pageindex
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/opt/pageindex:/app/src"
RUN useradd --create-home --uid 10001 app && mkdir -p /workspaces && chown -R app:app /app /workspaces
USER app
EXPOSE 8000
CMD ["uvicorn", "vectorless_rag.api:app", "--host", "0.0.0.0", "--port", "8000"]
