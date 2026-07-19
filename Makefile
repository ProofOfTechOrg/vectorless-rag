SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
UV_CACHE_DIR ?= .uv-cache
HELM ?= helm
DOCKER ?= docker
COMPOSE ?= docker compose
CHART := deploy/helm/vectorless-rag
ENV_FILE ?= .env
NAME ?= operator
SCOPES ?= chat,documents:read,admin:ingest
FORCE ?= false
PDF ?=
SERVICE ?=
TAIL ?= 200
DATASET ?= evaluation/release-v1.jsonl
DATASET_NAME ?= vectorless-rag-release-v1

export UV_CACHE_DIR

.PHONY: help env install format lint typecheck test test-unit test-integration \
	workflow-check migration-check compose-check helm-lint helm-template helm-check docker-build check ci \
	up down stop restart restart-app restart-langfuse restart-observability ps logs health \
	migrate bootstrap-key sync-corpus upload-document pilot-report evaluation-build \
	evaluation-seed secret-api-pepper secrets-langfuse

help: ## Show all supported project commands.
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target> [VARIABLE=value]\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Create .env from .env.example if absent and restrict its permissions.
	@if [[ -e .env ]]; then echo ".env already exists; leaving it unchanged"; else cp .env.example .env; echo "created .env"; fi
	@chmod 600 .env

install: ## Install the frozen Python development environment.
	$(UV) sync --frozen

format: ## Format Python and apply safe Ruff fixes.
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Check Ruff lint and formatting rules without modifying files.
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck: ## Run strict mypy checks.
	$(UV) run mypy src

test: ## Run the test suite with coverage; integration tests skip without their URLs.
	$(UV) run pytest --cov=vectorless_rag --cov-report=term-missing

test-unit: ## Run tests that do not require external infrastructure.
	$(UV) run pytest -m 'not integration'

test-integration: ## Run PostgreSQL integration tests; requires TEST_*_DATABASE_URL exports.
	@test -n "$${TEST_OWNER_DATABASE_URL:-}" || { echo "TEST_OWNER_DATABASE_URL is required" >&2; exit 2; }
	@test -n "$${TEST_DATABASE_URL:-}" || { echo "TEST_DATABASE_URL is required" >&2; exit 2; }
	$(UV) run pytest -m integration

workflow-check: ## Parse GitHub Actions workflow YAML.
	$(UV) run python -c 'from pathlib import Path; import yaml; yaml.safe_load(Path(".github/workflows/ci.yml").read_text())'

migration-check: ## Render the full Alembic migration offline.
	$(UV) run alembic upgrade head --sql >/dev/null

compose-check: ## Validate Compose using ENV_FILE (default: .env).
	@test -f "$(ENV_FILE)" || { echo "$(ENV_FILE) does not exist; run 'make env'" >&2; exit 2; }
	$(COMPOSE) --env-file "$(ENV_FILE)" config --quiet

helm-lint: ## Lint the Helm chart.
	$(HELM) lint $(CHART)

helm-template: ## Render the Helm chart with its default values.
	$(HELM) template test $(CHART) >/dev/null

helm-check: helm-lint helm-template ## Lint and render the Helm chart.

docker-build: ## Build the production application image.
	$(DOCKER) build -t vectorless-rag:test .

check: lint typecheck test workflow-check migration-check compose-check helm-check ## Run all static, test, migration, Compose, and Helm checks.

ci: ENV_FILE := .env.example
ci: check docker-build ## Run the complete CI-equivalent validation locally.

up: ## Build and start the complete Compose stack in the background.
	$(COMPOSE) up --build -d

down: ## Stop and remove Compose containers and networks, preserving volumes.
	$(COMPOSE) down

stop: ## Stop Compose containers without removing them.
	$(COMPOSE) stop

restart: ## Recreate the complete stack without removing volumes.
	$(COMPOSE) up -d --force-recreate

restart-app: ## Recreate only the API and ingestion worker.
	$(COMPOSE) up -d --force-recreate api worker

restart-langfuse: ## Recreate only Langfuse web and worker processes.
	$(COMPOSE) up -d --force-recreate langfuse-web langfuse-worker

restart-observability: ## Reload Langfuse credentials in Langfuse and application processes.
	$(COMPOSE) up -d --force-recreate langfuse-web langfuse-worker api worker

ps: ## Show Compose service status.
	$(COMPOSE) ps

logs: ## Follow logs; optionally set SERVICE=name and TAIL=lines.
	$(COMPOSE) logs --follow --tail="$(TAIL)" $(SERVICE)

health: ## Check the API and Langfuse public health endpoints.
	curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
	@printf '\n'
	curl --fail --silent --show-error http://127.0.0.1:3000/api/public/health
	@printf '\n'

migrate: ## Apply application migrations inside the API container.
	$(COMPOSE) exec -T api vectorless-rag migrate

bootstrap-key: ## Create a RAG API key; set NAME and optionally SCOPES.
	@$(COMPOSE) exec -T api vectorless-rag bootstrap-key --name "$(NAME)" --scopes "$(SCOPES)"

sync-corpus: ## Queue mounted PDFs; export VECTORLESS_RAG_ADMIN_KEY and optionally set FORCE=true.
	@test -n "$${VECTORLESS_RAG_ADMIN_KEY:-}" || { echo "VECTORLESS_RAG_ADMIN_KEY is required" >&2; exit 2; }
	@if [[ "$(FORCE)" != "true" && "$(FORCE)" != "false" ]]; then echo "FORCE must be true or false" >&2; exit 2; fi
	@curl --fail --silent --show-error -X POST http://127.0.0.1:8000/v1/admin/ingestion/sync \
		-H "X-API-Key: $${VECTORLESS_RAG_ADMIN_KEY}" \
		-H "Content-Type: application/json" \
		-d '{"force": $(FORCE)}'
	@printf '\n'

upload-document: ## Upload and queue PDF; set PDF and export VECTORLESS_RAG_ADMIN_KEY.
	@test -n "$(PDF)" || { echo "PDF=/path/to/file.pdf is required" >&2; exit 2; }
	@test -f "$(PDF)" || { echo "PDF does not exist: $(PDF)" >&2; exit 2; }
	@test -n "$${VECTORLESS_RAG_ADMIN_KEY:-}" || { echo "VECTORLESS_RAG_ADMIN_KEY is required" >&2; exit 2; }
	@curl --fail --silent --show-error -X POST http://127.0.0.1:8000/v1/admin/documents \
		-H "X-API-Key: $${VECTORLESS_RAG_ADMIN_KEY}" \
		-F "file=@$(PDF)"
	@printf '\n'

pilot-report: ## Print the ten-document ingestion pilot report.
	@$(COMPOSE) exec -T api vectorless-rag pilot-report

evaluation-build: ## Rebuild the deterministic local evaluation dataset.
	$(UV) run python scripts/build_evaluation_dataset.py

evaluation-seed: ## Upload DATASET to Langfuse using the API container credentials.
	@test -f "$(DATASET)" || { echo "DATASET does not exist: $(DATASET)" >&2; exit 2; }
	$(COMPOSE) exec -T api python scripts/seed_evaluation.py /dev/stdin --name "$(DATASET_NAME)" < "$(DATASET)"

secret-api-pepper: ## Print a generated API_KEY_PEPPER assignment for .env.
	@printf 'API_KEY_PEPPER=%s\n' "$$(openssl rand -hex 32)"

secrets-langfuse: ## Print generated Langfuse project, service, and initial-user assignments.
	@printf 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY=lf_pk_%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_INIT_PROJECT_SECRET_KEY=lf_sk_%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_NEXTAUTH_SECRET=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_SALT=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_ENCRYPTION_KEY=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_INIT_USER_PASSWORD=%s\n' "$$(openssl rand -hex 16)"
