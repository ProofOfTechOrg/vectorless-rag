SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
UV_CACHE_DIR ?= .uv-cache
PNPM ?= pnpm
HELM ?= helm
DOCKER ?= docker
COMPOSE ?= docker compose
API_DIR := apps/api
WEB_DIR := apps/web
CHART := infra/helm/vectorless-rag
ENV_FILE ?= .env
ARXIV_PDF_HOST_DIR ?= ./arxiv-pdfs
NAME ?= operator
SCOPES ?= chat,documents:read,admin:ingest
FORCE ?= false
PDF ?=
SERVICE ?=
TAIL ?= 200
DATASET ?= apps/api/evaluation/release-v1.jsonl
DATASET_NAME ?= vectorless-rag-release-v1

export UV_CACHE_DIR

.PHONY: help env install api-install web-install format api-format web-format lint api-lint web-lint \
	typecheck api-typecheck web-typecheck test api-test web-test test-unit test-integration \
	api-build web-build playwright playwright-live react-doctor react-doctor-diff astryx-doctor workflow-check migration-check \
	compose-check helm-lint helm-template helm-check docker-build docker-build-api docker-build-web \
	check api-check web-check deploy-check ci up down stop restart restart-app restart-web \
	restart-langfuse restart-observability ps logs health web-dev migrate bootstrap-key \
	download-corpus sync-corpus upload-document pilot-report evaluation-build evaluation-seed \
	secret-api-pepper secret-session secrets-langfuse

help: ## Show all supported project commands.
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target> [VARIABLE=value]\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Create .env from .env.example if absent and restrict its permissions.
	@if [[ -e .env ]]; then echo ".env already exists; leaving it unchanged"; else cp .env.example .env; echo "created .env"; fi
	@chmod 600 .env

install: api-install web-install ## Install all frozen development dependencies.
api-install: ## Install the frozen Python environment under apps/api.
	$(UV) sync --project $(API_DIR) --frozen
web-install: ## Install the frozen root pnpm workspace.
	$(PNPM) install --frozen-lockfile

format: api-format web-format ## Format API and web sources.
api-format:
	$(UV) run --project $(API_DIR) ruff format $(API_DIR)
	$(UV) run --project $(API_DIR) ruff check --fix $(API_DIR)
web-format:
	$(PNPM) --filter @vectorless-rag/web exec eslint . --fix

lint: api-lint web-lint ## Lint API and web sources.
api-lint:
	$(UV) run --project $(API_DIR) ruff check $(API_DIR)
	$(UV) run --project $(API_DIR) ruff format --check $(API_DIR)
web-lint:
	$(PNPM) --filter @vectorless-rag/web lint

typecheck: api-typecheck web-typecheck ## Typecheck API and web sources.
api-typecheck:
	cd $(API_DIR) && $(UV) run basedpyright
web-typecheck:
	$(PNPM) --filter @vectorless-rag/web typecheck

test: api-test web-test ## Run API and web unit tests.
api-test:
	$(UV) run --project $(API_DIR) pytest $(API_DIR)/tests --cov=vectorless_rag --cov-report=term-missing
web-test:
	$(PNPM) --filter @vectorless-rag/web test
test-unit: ## Run Python tests that require no external infrastructure.
	$(UV) run --project $(API_DIR) pytest $(API_DIR)/tests -m 'not integration'
test-integration: ## Run PostgreSQL integration tests with TEST_*_DATABASE_URL.
	@test -n "$${TEST_OWNER_DATABASE_URL:-}" || { echo "TEST_OWNER_DATABASE_URL is required" >&2; exit 2; }
	@test -n "$${TEST_DATABASE_URL:-}" || { echo "TEST_DATABASE_URL is required" >&2; exit 2; }
	$(UV) run --project $(API_DIR) pytest $(API_DIR)/tests -m integration

api-build: ## Build the Python distribution.
	$(UV) build --project $(API_DIR)
web-build: ## Build the TanStack Start production bundle.
	$(PNPM) --filter @vectorless-rag/web build
playwright: ## Run deterministic Playwright against the local mock upstream.
	$(PNPM) --filter @vectorless-rag/web test:e2e
playwright-live: ## Run Playwright against PLAYWRIGHT_BASE_URL and a live stack.
	PLAYWRIGHT_LIVE=1 $(PNPM) --filter @vectorless-rag/web test:e2e
react-doctor: ## Run the authoritative full React Doctor 100/100 gate.
	$(PNPM) --filter @vectorless-rag/web react-doctor
react-doctor-diff: ## Run React Doctor against files changed from the current PR base.
	$(PNPM) --filter @vectorless-rag/web react-doctor:diff
astryx-doctor: ## Diagnose ASTRYX package, peer, theme, and agent setup.
	$(PNPM) --filter @vectorless-rag/web astryx:doctor

workflow-check: ## Parse GitHub Actions workflow YAML.
	$(UV) run --project $(API_DIR) python -c 'from pathlib import Path; import yaml; yaml.safe_load(Path(".github/workflows/ci.yml").read_text())'
migration-check: ## Render the complete Alembic migration offline.
	cd $(API_DIR) && $(UV) run alembic upgrade head --sql >/dev/null
compose-check: ## Validate Compose using ENV_FILE (default .env).
	@test -f "$(ENV_FILE)" || { echo "$(ENV_FILE) does not exist; run make env" >&2; exit 2; }
	$(COMPOSE) --env-file "$(ENV_FILE)" config --quiet
helm-lint:
	$(HELM) lint $(CHART)
helm-template:
	$(HELM) template test $(CHART) >/dev/null
helm-check: helm-lint helm-template ## Lint and render the Helm chart.
docker-build-api: ## Build the production FastAPI image.
	$(DOCKER) build -t vectorless-rag-api:test $(API_DIR)
docker-build-web: ## Build the production frontend image.
	$(DOCKER) build -f $(WEB_DIR)/Dockerfile -t vectorless-rag-web:test .
docker-build: docker-build-api docker-build-web ## Build both production images.

api-check: api-lint api-typecheck api-test migration-check ## Validate the API package.
web-check: web-lint web-typecheck web-test react-doctor web-build astryx-doctor ## Validate the web package.
deploy-check: workflow-check compose-check helm-check ## Validate CI and deployment manifests.
check: api-check web-check deploy-check ## Validate the full repository without browser or image builds.
ci: ENV_FILE := .env.example
ci: check playwright docker-build ## Run the complete CI-equivalent validation.

up: ## Build and start the complete Compose stack without removing volumes.
	$(COMPOSE) up --build -d
down: ## Stop and remove containers and networks while preserving volumes.
	$(COMPOSE) down
stop: ## Stop Compose containers without removing them.
	$(COMPOSE) stop
restart: ## Recreate the complete stack without removing volumes.
	$(COMPOSE) up -d --force-recreate
restart-app: ## Rebuild and recreate API and worker without removing volumes.
	$(COMPOSE) up -d --build --force-recreate api worker
restart-web: ## Rebuild and recreate only the frontend.
	$(COMPOSE) up -d --build --force-recreate web
restart-langfuse:
	$(COMPOSE) up -d --force-recreate langfuse-web langfuse-worker
restart-observability:
	$(COMPOSE) up -d --force-recreate langfuse-web langfuse-worker api worker
ps:
	$(COMPOSE) ps
logs:
	$(COMPOSE) logs --follow --tail="$(TAIL)" $(SERVICE)
health: ## Check frontend, API, and Langfuse public health endpoints.
	curl --fail --silent --show-error http://127.0.0.1:$${FRONTEND_PORT:-3001}/healthz
	@printf '\n'
	curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
	@printf '\n'
	curl --fail --silent --show-error http://127.0.0.1:3000/api/public/health
	@printf '\n'
web-dev: ## Run web locally against the live Compose API.
	VECTORLESS_API_URL=http://127.0.0.1:8000 APP_ORIGIN=http://localhost:3001 $(PNPM) --filter @vectorless-rag/web dev

migrate:
	$(COMPOSE) exec -T api vectorless-rag migrate
bootstrap-key:
	@$(COMPOSE) exec -T api vectorless-rag bootstrap-key --name "$(NAME)" --scopes "$(SCOPES)"
download-corpus: ## Download missing PDFs from the tracked arXiv corpus.
	apps/api/scripts/download-arxiv-pdfs.sh "$(ARXIV_PDF_HOST_DIR)"
sync-corpus:
	@test -n "$${VECTORLESS_RAG_ADMIN_KEY:-}" || { echo "VECTORLESS_RAG_ADMIN_KEY is required" >&2; exit 2; }
	@if [[ "$(FORCE)" != "true" && "$(FORCE)" != "false" ]]; then echo "FORCE must be true or false" >&2; exit 2; fi
	@curl --fail --silent --show-error -X POST http://127.0.0.1:8000/v1/admin/ingestion/sync -H "X-API-Key: $${VECTORLESS_RAG_ADMIN_KEY}" -H "Content-Type: application/json" -d '{"force": $(FORCE)}'
	@printf '\n'
upload-document:
	@test -n "$(PDF)" || { echo "PDF=/path/to/file.pdf is required" >&2; exit 2; }
	@test -f "$(PDF)" || { echo "PDF does not exist: $(PDF)" >&2; exit 2; }
	@test -n "$${VECTORLESS_RAG_ADMIN_KEY:-}" || { echo "VECTORLESS_RAG_ADMIN_KEY is required" >&2; exit 2; }
	@curl --fail --silent --show-error -X POST http://127.0.0.1:8000/v1/admin/documents -H "X-API-Key: $${VECTORLESS_RAG_ADMIN_KEY}" -F "file=@$(PDF)"
	@printf '\n'
pilot-report:
	@$(COMPOSE) exec -T api vectorless-rag pilot-report
evaluation-build:
	$(UV) run --project $(API_DIR) python $(API_DIR)/scripts/build_evaluation_dataset.py
evaluation-seed:
	@test -f "$(DATASET)" || { echo "DATASET does not exist: $(DATASET)" >&2; exit 2; }
	$(COMPOSE) exec -T api python scripts/seed_evaluation.py /dev/stdin --name "$(DATASET_NAME)" < "$(DATASET)"

secret-api-pepper:
	@printf 'API_KEY_PEPPER=%s\n' "$$(openssl rand -hex 32)"
secret-session: ## Print a generated 32-byte SESSION_SECRET assignment.
	@printf 'SESSION_SECRET=%s\n' "$$(openssl rand -hex 32)"
secrets-langfuse:
	@printf 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY=lf_pk_%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_INIT_PROJECT_SECRET_KEY=lf_sk_%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_NEXTAUTH_SECRET=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_SALT=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_ENCRYPTION_KEY=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_INIT_USER_PASSWORD=%s\n' "$$(openssl rand -hex 16)"
