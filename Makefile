SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
UV_CACHE_DIR ?= .uv-cache
PYTHON ?= python3
PNPM ?= pnpm
HELM ?= helm
DOCKER ?= docker
COMPOSE ?= docker compose
DEV_COMPOSE = $(COMPOSE) --env-file "$(ENV_FILE)" -f compose.yaml -f compose.dev.yaml
API_DIR := apps/api
WEB_DIR := apps/web
CHART := infra/helm/vectorless-rag
ENV_FILE ?= .env
ARXIV_PDF_HOST_DIR ?= ./arxiv-pdfs
NAME ?= operator
SCOPES ?= chat,documents:read,admin:ingest
FORCE ?= false
DAYS ?= 30
PDF ?=
SERVICE ?=
TAIL ?= 200
DATASET ?= apps/api/evaluation/pageindex-v2-regression.jsonl
DATASET_NAME ?= vectorless-rag-pageindex-v2-regression
INDEX_HASH ?=
PAGEINDEX_SOURCE ?= /opt/pageindex
PILOT_OUTPUT ?= apps/api/pilots/run/result.json

export UV_CACHE_DIR

.PHONY: help env env-sync env-check init install api-install web-install format api-format web-format lint api-lint web-lint \
	typecheck api-typecheck web-typecheck test api-test web-test test-unit test-integration \
	test-integration-stack docs-check \
	api-build web-build web-production-smoke playwright-install playwright playwright-live react-doctor react-doctor-diff astryx-doctor workflow-check migration-check \
	compose-check helm-lint helm-template helm-check docker-build docker-build-api docker-build-web \
	check api-check web-check deploy-check ci up rebuild dev dev-build dev-health down stop restart restart-app restart-web \
	restart-langfuse restart-observability ps logs health web-dev migrate bootstrap-key \
	download-corpus sync-corpus upload-document pilot-check pilot-run pilot-report query-cost-report artifact-verify artifact-gc pageindex-patch-test evaluation-build evaluation-check evaluation-review-pack evaluation-review evaluation-review-status evaluation-seed evaluation-restart evaluation-run evaluation-freeze evaluation-execute evaluation-report \
	secret-api-pepper secret-session secrets-langfuse

help: ## Show all supported project commands.
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target> [VARIABLE=value]\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Create .env from .env.example if absent and restrict its permissions.
	@if [[ -e .env ]]; then echo ".env already exists; leaving it unchanged"; else cp .env.example .env; echo "created .env"; fi
	@chmod 600 .env

init: ## Create a mode-600 .env with generated local secrets without overwriting.
	$(PYTHON) scripts/init_env.py

env-sync: ## Add keys .env is missing from .env.example (ADOPT=KEY,KEY to take example values).
	$(PYTHON) scripts/sync_env.py $(if $(ADOPT),--adopt "$(ADOPT)",)
env-check: ## Report .env drift against .env.example without writing.
	$(PYTHON) scripts/sync_env.py --check

install: api-install web-install ## Install all frozen development dependencies.
api-install: ## Install the frozen Python environment under apps/api.
	$(UV) sync --project $(API_DIR) --frozen
web-install: ## Install the frozen root pnpm workspace.
	$(PNPM) install --frozen-lockfile

format: api-format web-format ## Format API and web sources.
api-format: ## Format and autofix Python sources.
	$(UV) run --project $(API_DIR) ruff format $(API_DIR) scripts
	$(UV) run --project $(API_DIR) ruff check --fix $(API_DIR) scripts
web-format: ## Autofix lint findings in web sources.
	$(PNPM) --filter @vectorless-rag/web exec eslint . --fix

lint: api-lint web-lint ## Lint API and web sources.
api-lint: ## Lint and check formatting for Python sources.
	$(UV) run --project $(API_DIR) ruff check $(API_DIR) scripts
	$(UV) run --project $(API_DIR) ruff format --check $(API_DIR) scripts
web-lint: ## Lint web sources.
	$(PNPM) --filter @vectorless-rag/web lint

typecheck: api-typecheck web-typecheck ## Typecheck API and web sources.
api-typecheck: ## Typecheck the Python package with Basedpyright.
	cd $(API_DIR) && $(UV) run basedpyright
web-typecheck: ## Typecheck the web package with TypeScript.
	$(PNPM) --filter @vectorless-rag/web typecheck

test: api-test web-test ## Run API and web unit tests.
api-test: ## Run API tests with coverage.
	cd $(API_DIR) && $(UV) run pytest tests --cov=vectorless_rag --cov-report=term-missing
web-test: ## Run web unit tests.
	$(PNPM) --filter @vectorless-rag/web test
test-unit: ## Run Python tests that require no external infrastructure.
	cd $(API_DIR) && $(UV) run pytest tests -m 'not integration'
test-integration: ## Run PostgreSQL integration tests with TEST_*_DATABASE_URL.
	@test -n "$${TEST_OWNER_DATABASE_URL:-}" || { echo "TEST_OWNER_DATABASE_URL is required" >&2; exit 2; }
	@test -n "$${TEST_DATABASE_URL:-}" || { echo "TEST_DATABASE_URL is required" >&2; exit 2; }
	cd $(API_DIR) && $(UV) run pytest tests -m integration
test-integration-stack: ## Run integration tests against an isolated disposable PostgreSQL project.
	UV="$(UV)" DOCKER="$(DOCKER)" MAKE="$(MAKE)" scripts/test-integration-stack.sh

docs-check: ## Validate community files, Markdown links, anchors, fences, and the documentation index.
	$(PYTHON) scripts/docs_check.py

api-build: ## Build the Python distribution.
	$(UV) build --project $(API_DIR)
web-build: ## Build the TanStack Start production bundle.
	$(PNPM) --filter @vectorless-rag/web build
web-production-smoke: web-build ## Verify the built SSR document serves all referenced CSS and JavaScript assets.
	$(PNPM) --filter @vectorless-rag/web production-smoke
playwright-install: ## Install Chromium and its operating-system dependencies for Playwright.
	$(PNPM) --filter @vectorless-rag/web exec playwright install chromium
	@if ! $(PNPM) --filter @vectorless-rag/web exec node scripts/verify-playwright-install.mjs; then \
		$(PNPM) --filter @vectorless-rag/web exec playwright install-deps chromium; \
		$(PNPM) --filter @vectorless-rag/web exec node scripts/verify-playwright-install.mjs; \
	fi
playwright: ## Run deterministic Playwright against the local mock upstream.
	$(PNPM) --filter @vectorless-rag/web test:e2e
playwright-live: ## Run Playwright against a live stack with an explicit URL and process-only key.
	@test -n "$${PLAYWRIGHT_BASE_URL:-}" || { echo "PLAYWRIGHT_BASE_URL is required" >&2; exit 2; }
	@test -n "$${PLAYWRIGHT_API_KEY:-}" || { echo "PLAYWRIGHT_API_KEY is required" >&2; exit 2; }
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
	$(DEV_COMPOSE) config --quiet
helm-lint: ## Lint the Helm chart.
	$(HELM) lint $(CHART)
helm-template: ## Render the Helm chart with default values.
	$(HELM) template test $(CHART) >/dev/null
helm-check: helm-lint helm-template ## Lint and render the Helm chart.
docker-build-api: ## Build the production FastAPI image.
	$(DOCKER) build -t vectorless-rag-api:test $(API_DIR)
docker-build-web: ## Build the production frontend image.
	$(DOCKER) build -f $(WEB_DIR)/Dockerfile -t vectorless-rag-web:test .
docker-build: docker-build-api docker-build-web ## Build both production images.

api-check: api-lint api-typecheck api-test migration-check ## Validate the API package.
web-check: web-lint web-typecheck web-test react-doctor web-production-smoke astryx-doctor ## Validate the web package.
deploy-check: workflow-check compose-check helm-check ## Validate CI and deployment manifests.
check: docs-check api-check web-check deploy-check ## Validate the full repository without browser or image builds.
ci: ENV_FILE := .env.example
ci: check test-integration-stack playwright-install playwright docker-build ## Run the complete CI-equivalent validation.

up: ## Start the complete Compose stack in the background without forcing rebuilds.
	$(COMPOSE) up -d
rebuild: ## Rebuild and recreate the complete Compose stack in the background.
	$(COMPOSE) up -d --build --force-recreate
dev: env ## Run the current development stack with source reload and rebuild watches.
	$(DEV_COMPOSE) up --build --watch --remove-orphans
dev-build: env ## Rebuild the development API, worker, and web images.
	$(DEV_COMPOSE) build api worker web
dev-health: ## Check that development web health and the Vite client are both available.
	curl --fail --silent --show-error http://127.0.0.1:$${FRONTEND_PORT:-3001}/healthz
	@printf '\n'
	curl --fail --silent --show-error http://127.0.0.1:$${FRONTEND_PORT:-3001}/@vite/client >/dev/null
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
restart-langfuse: ## Recreate the Langfuse web and worker services.
	$(COMPOSE) up -d --force-recreate langfuse-web langfuse-worker
restart-observability: ## Recreate Langfuse, API, and worker services.
	$(COMPOSE) up -d --force-recreate langfuse-web langfuse-worker api worker
ps: ## Show Compose service status.
	$(COMPOSE) ps
logs: ## Follow Compose logs, optionally limited by SERVICE and TAIL.
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

migrate: ## Apply database migrations in the API container.
	$(COMPOSE) exec -T api vectorless-rag migrate
bootstrap-key: ## Mint an API key with NAME and SCOPES in the API container.
	@$(COMPOSE) exec -T api vectorless-rag bootstrap-key --name "$(NAME)" --scopes "$(SCOPES)"
download-corpus: ## Download missing PDFs from the tracked arXiv corpus.
	apps/api/scripts/download-arxiv-pdfs.sh "$(ARXIV_PDF_HOST_DIR)"
sync-corpus: ## Queue mounted corpus PDFs with VECTORLESS_RAG_ADMIN_KEY.
	@test -n "$${VECTORLESS_RAG_ADMIN_KEY:-}" || { echo "VECTORLESS_RAG_ADMIN_KEY is required" >&2; exit 2; }
	@if [[ "$(FORCE)" != "true" && "$(FORCE)" != "false" ]]; then echo "FORCE must be true or false" >&2; exit 2; fi
	@curl --fail --silent --show-error -X POST http://127.0.0.1:8000/v1/admin/ingestion/sync -H "X-API-Key: $${VECTORLESS_RAG_ADMIN_KEY}" -H "Content-Type: application/json" -d '{"force": $(FORCE)}'
	@printf '\n'
upload-document: ## Upload PDF through FastAPI with VECTORLESS_RAG_ADMIN_KEY.
	@test -n "$(PDF)" || { echo "PDF=/path/to/file.pdf is required" >&2; exit 2; }
	@test -f "$(PDF)" || { echo "PDF does not exist: $(PDF)" >&2; exit 2; }
	@test -n "$${VECTORLESS_RAG_ADMIN_KEY:-}" || { echo "VECTORLESS_RAG_ADMIN_KEY is required" >&2; exit 2; }
	@curl --fail --silent --show-error -X POST http://127.0.0.1:8000/v1/admin/documents -H "X-API-Key: $${VECTORLESS_RAG_ADMIN_KEY}" -F "file=@$(PDF)"
	@printf '\n'
pilot-check: ## Validate the frozen 25-document pilot manifest offline.
	cd $(API_DIR) && $(UV) run python scripts/check_pageindex_pilot.py
pilot-run: ## Run the frozen pilot with an explicit ten-dollar confirmation.
	@test -z "$$(git status --porcelain)" || { echo "pilot-run requires a clean worktree" >&2; exit 2; }
	@release_sha="$$(git rev-parse HEAD)"; \
		output="$(PILOT_OUTPUT)"; \
		output_dir="$$(dirname "$$output")"; \
		mkdir -p "$$(dirname "$$output_dir")"; \
		mkdir "$$output_dir"; \
		temporary="$$output_dir/.$$(basename "$$output").tmp"; \
		: > "$$temporary"; \
		if $(COMPOSE) exec -T api python scripts/run_pageindex_pilot.py \
			--apply --confirm-cost-cap 10 --release-sha "$$release_sha" --output - \
			> "$$temporary"; then \
			mv "$$temporary" "$$output"; \
			cat "$$output"; \
		else \
			echo "pilot failed; reserved output remains at $$output_dir" >&2; \
			exit 1; \
		fi
pilot-report: ## Print the configured stratified-pilot report from the API container.
	@$(COMPOSE) exec -T api vectorless-rag pilot-report
query-cost-report: ## Print aggregate query cost data for DAYS (default 30).
	@$(COMPOSE) exec -T api vectorless-rag query-cost-report --days "$(DAYS)"
artifact-verify: ## Verify all active PageIndex-v2 manifests and referenced objects.
	@$(COMPOSE) exec -T api vectorless-rag artifact-verify
artifact-gc: ## Dry-run PageIndex-v2 garbage collection.
	@$(COMPOSE) exec -T api vectorless-rag artifact-gc
pageindex-patch-test: ## Run provider-free checks against a patched PageIndex checkout.
	cd $(API_DIR) && $(UV) run python scripts/test_pageindex_patch.py "$(PAGEINDEX_SOURCE)"
evaluation-build: ## Build the deterministic evaluation dataset.
	$(UV) run --project $(API_DIR) python $(API_DIR)/scripts/build_evaluation_dataset.py
evaluation-check: ## Validate the tracked 120-case PageIndex suite without PDFs or services.
	cd $(API_DIR) && $(UV) run python scripts/check_evaluation_suite.py
evaluation-review-pack: ## Rebuild the local PageIndex evaluation review pack.
	cd $(API_DIR) && $(UV) run python scripts/build_evaluation_review_pack.py
evaluation-review: ## Review PageIndex cases interactively (REVIEWER=name, optional STRATUM=).
	@test -n "$(REVIEWER)" || { echo "set REVIEWER=<your identity>" >&2; exit 2; }
	cd $(API_DIR) && $(UV) run python scripts/review_evaluation.py \
		--reviewer "$(REVIEWER)" $(if $(STRATUM),--stratum "$(STRATUM)",)
evaluation-review-status: ## Show PageIndex review progress.
	cd $(API_DIR) && $(UV) run python scripts/review_evaluation.py --status
evaluation-seed: ## Seed the reviewed PageIndex evaluation suite into Langfuse.
	@test -f "$(DATASET)" || { echo "DATASET does not exist: $(DATASET)" >&2; exit 2; }
	$(COMPOSE) exec -T api python scripts/seed_evaluation.py /dev/stdin \
		--suite pageindex-v2 --corpus evaluation/pageindex-v2-corpus.json \
		--name "$(DATASET_NAME)" < "$(DATASET)"
evaluation-restart: ## Rebuild API and worker with the clean HEAD as the runtime release.
	@test -z "$$(git status --porcelain)" || { echo "evaluation rebuild requires a clean worktree" >&2; exit 2; }
	@release_sha="$$(git rev-parse HEAD)"; \
		RELEASE="$$release_sha" $(COMPOSE) up -d --build --force-recreate api worker
evaluation-run: ## Prepare a frozen PageIndex experiment (INDEX_HASH=…, MAX_COST=…).
	@test -n "$(INDEX_HASH)" || { echo "INDEX_HASH=<sha256> is required" >&2; exit 2; }
	@test -n "$(MAX_COST)" || { echo "MAX_COST=<usd> is required; it becomes the run's ceiling" >&2; exit 2; }
	@release_sha="$$(git rev-parse HEAD)"; \
		RELEASE="$$release_sha" $(UV) run --project $(API_DIR) \
		python $(API_DIR)/scripts/run_evaluation.py \
			--index-hash "$(INDEX_HASH)" --max-cost-usd "$(MAX_COST)" $(if $(CONCURRENCY),--concurrency $(CONCURRENCY),)
evaluation-freeze: ## Print the index digest for evaluation-run INDEX_HASH.
	$(COMPOSE) exec -T api python scripts/freeze_index.py
evaluation-execute: ## Execute prepared trials against DeepSeek (LIMIT=n, DRY_RUN=1).
	$(COMPOSE) exec -T --user "$$(id -u):$$(id -g)" api python scripts/execute_evaluation.py \
		$(if $(LIMIT),--limit $(LIMIT),) $(if $(DRY_RUN),--dry-run,) $(if $(CONCURRENCY),--concurrency $(CONCURRENCY),)
evaluation-report: ## Build the local report for the latest frozen evaluation experiment.
	cd $(API_DIR) && $(UV) run python scripts/report_evaluation.py

secret-api-pepper: ## Print a generated API_KEY_PEPPER assignment.
	@printf 'API_KEY_PEPPER=%s\n' "$$(openssl rand -hex 32)"
secret-session: ## Print a generated 32-byte SESSION_SECRET assignment.
	@printf 'SESSION_SECRET=%s\n' "$$(openssl rand -hex 32)"
secrets-langfuse: ## Print generated Langfuse secret assignments.
	@printf 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY=lf_pk_%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_INIT_PROJECT_SECRET_KEY=lf_sk_%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_NEXTAUTH_SECRET=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_SALT=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_ENCRYPTION_KEY=%s\n' "$$(openssl rand -hex 32)"
	@printf 'LANGFUSE_INIT_USER_PASSWORD=%s\n' "$$(openssl rand -hex 16)"
