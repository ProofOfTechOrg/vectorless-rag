<!-- Content type: Reference -->
<!-- Content plan: Goal: identify every supported Make command and its operator contract. Audience: contributors and operators. Content: setup, development, tests, lifecycle, corpus, evaluation, and diagnostics. Open questions: none. -->

# Command reference

This page lists every public Make target and the variables that alter operator commands. Run all commands from the repository root. `make help` generates its target list from the Makefile.

| Area | Commands |
| --- | --- |
| Discovery and environment | `make help`, `make env`, `make init` |
| Dependencies | `make install`, `make api-install`, `make web-install` |
| Formatting | `make format`, `make api-format`, `make web-format` |
| Linting | `make lint`, `make api-lint`, `make web-lint` |
| Type checking | `make typecheck`, `make api-typecheck`, `make web-typecheck` |
| Unit and integration tests | `make test`, `make api-test`, `make web-test`, `make test-unit`, `make test-integration`, `make test-integration-stack` |
| Package builds | `make api-build`, `make web-build`, `make web-production-smoke` |
| Browser tests | `make playwright-install`, `make playwright`, `make playwright-live PLAYWRIGHT_BASE_URL=http://127.0.0.1:3001` |
| Web diagnostics | `make react-doctor`, `make react-doctor-diff`, `make astryx-doctor` |
| Documentation and manifest validation | `make docs-check`, `make workflow-check`, `make migration-check`, `make compose-check`, `make helm-lint`, `make helm-template`, `make helm-check` |
| Image builds | `make docker-build-api`, `make docker-build-web`, `make docker-build` |
| Validation suites | `make api-check`, `make web-check`, `make deploy-check`, `make check ENV_FILE=.env.example`, `make ci` |
| Lifecycle | `make up`, `make rebuild`, `make dev`, `make dev-build`, `make dev-health`, `make down`, `make stop`, `make restart`, `make restart-app`, `make restart-web`, `make restart-langfuse`, `make restart-observability`, `make ps`, `make logs SERVICE=worker`, `make health`, `make web-dev` |
| Corpus | `make download-corpus`, `make download-corpus ARXIV_PDF_HOST_DIR=/srv/vectorless/arxiv-pdfs` |
| Operator | `make migrate`, `make bootstrap-key NAME=operator`, `make sync-corpus`, `make upload-document PDF=/path/file.pdf`, `make pilot-check`, `make pilot-run`, `make pilot-report`, `make artifact-verify`, `make artifact-gc` |
| Evaluation | `make evaluation-build`, `make evaluation-check`, `make evaluation-review-pack`, `make evaluation-review REVIEWER=…`, `make evaluation-review-status`, `make evaluation-seed`, `make evaluation-run INDEX_HASH=…`, `make evaluation-execute LIMIT=10`, `make evaluation-report` |
| Secret generation | `make secret-api-pepper`, `make secret-session`, `make secrets-langfuse` |

`make pilot-run` writes its immutable result to `apps/api/pilots/run/result.json`. Set `PILOT_OUTPUT=path/to/result.json` to select another new output directory. The target fails before provider work when that directory already exists.

`make init` atomically creates a mode-600 `.env` with generated local infrastructure and session secrets. It leaves `DEEPSEEK_API_KEY` blank and refuses to overwrite an existing file.

`make test-integration-stack` uses port `55432` and a unique disposable Compose project. Set `TEST_POSTGRES_PORT` to another unused port. The cleanup trap removes only that project and its volumes.

`make docs-check` validates required community files, local Markdown targets and anchors, content declarations, language-tagged fences, and this documentation index.

`make evaluation-seed` streams the reviewed PageIndex suite into the API container with an explicit suite selector and corpus manifest. Direct script callers must pass `--suite pageindex-v2` when the dataset arrives through `/dev/stdin`.

`make playwright-live` exits with status 2 unless both `PLAYWRIGHT_BASE_URL` and the process-only `PLAYWRIGHT_API_KEY` are set. Use the [disposable live-stack procedure](operator-guide.md#verify-a-disposable-live-stack) to create both values without saving the key.

`make ci` runs the isolated PostgreSQL integration gate and installs Playwright Chromium with its operating-system dependencies before browser tests.

## Run the local development stack

Use the development Compose override when actively changing API, worker, or web code. Docker Compose 2.22 or later is required.

```bash
make dev
```

The command reads `.env`, builds the current images, runs in the foreground, watches rebuild inputs, and displays logs from the complete stack. Stop it with Ctrl+C. Named volumes remain available for the next run. Pass `ENV_FILE=path/to/env` to select another environment file.

Python changes restart Uvicorn and the ingestion worker. Web source changes use Vite hot module replacement. Changes to `apps/web/package.json`, the root `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`, or `apps/web/Dockerfile` automatically rebuild and recreate the web service. Confirm that Vite is serving the frontend with:

```bash
make dev-health
```

`make dev-build` remains available as an optional image prebuild or troubleshooting command. Migration changes require `make migrate` or a development-stack restart. Changes to `.env`, `compose.yaml`, or `compose.dev.yaml` require stopping and rerunning `make dev` so Compose re-resolves service configuration.

Use `make web-dev` when you want only the web process on the host. It runs the Start development server on port 3001 against FastAPI on port 8000 and requires `SESSION_SECRET` in your shell or `.env`.

`make up` starts the immutable deployment-style Compose stack in the background without hot reload or forced rebuilds. Run `make rebuild` to rebuild every local application image and force-recreate the complete stack. Use `make restart-app` or `make restart-web` when only one application area needs a new production-style image. All four commands preserve named volumes.

`make web-production-smoke` builds the frontend, starts its production server on an isolated local port, and verifies that `/connect` serves every same-origin stylesheet and module script referenced by its SSR HTML.

## Download the test corpus

This procedure downloads the tracked test corpus without starting services or queuing ingestion.

Use `make download-corpus` to fetch the 537 PDFs represented by the tracked corpus manifest. The current set requires approximately 1.5 GB of storage. Install Bash and `curl`, then confirm that the destination directory is writable.

Download missing PDFs into the default `arxiv-pdfs` directory:

```bash
make download-corpus
```

Pass the same host directory configured for Compose when you store the corpus elsewhere:

```bash
make download-corpus ARXIV_PDF_HOST_DIR=/srv/vectorless/arxiv-pdfs
```

`make` does not load `ARXIV_PDF_HOST_DIR` from `.env` for this target. Pass the value on the command line when `.env` uses a custom path.

The downloader handles each manifest entry as follows:

- A missing ID downloads from `export.arxiv.org` and resolves to the latest available revision.
- A valid existing `arxiv_id.pdf` is skipped without a network request.
- An invalid response is removed before it can become a final PDF.
- An existing invalid file, directory, or broken symlink remains untouched and counts as a failure.
- Other IDs continue after a failure, and the final summary reports downloaded, skipped, and failed counts.

The script writes each response to a temporary file, validates the `%PDF-` signature, and installs the result with an atomic rename. It retries transient transfer failures and follows [arXiv’s bulk-access guidance](https://info.arxiv.org/help/bulk_data.html) by pausing after every four requests.

The command returns a nonzero status if any ID fails. Correct or remove the invalid destination path, resolve any network problem, and rerun the same command. Valid files from the previous run are skipped.

## Ingest downloaded PDFs

This procedure queues downloaded PDFs after you deliberately start the stack and supply an administrative key.

Downloading the corpus does not start services or queue ingestion. Start the stack, create an administrative key, and then run `make sync-corpus` when you intend to ingest the mounted PDFs.

The default 25-document gate still applies. The tracked pilot is manifest-driven and has a database-enforced $10 ceiling. Do not enable full-corpus indexing until the v2 pilot is accepted and separately authorized.

Operator curl targets require `VECTORLESS_RAG_ADMIN_KEY` in the process environment. They never read the browser session. `make down` preserves named volumes; do not use `docker compose down -v` unless a full local data reset is intended.

## React Doctor

These commands run the full-project gate or a changed-files diagnostic against the immutable React Doctor Git commit configured in `apps/web/package.json`.

`make react-doctor` (or `pnpm react-doctor`) runs the authoritative full-project React Doctor scan. CI and `make web-check` require this scan to remain at 100/100 with warnings treated as blocking findings.

`make react-doctor-diff` (or `pnpm react-doctor:diff`) scans only files changed from the current branch's open pull request base. The resolver prefers `origin/<base>`, falls back to a local base branch, and otherwise uses React Doctor's `parent` fork-point heuristic. It does not fetch or mutate remote refs.

Husky runs the changed-files scan before every push. The hook fails closed on findings or tool errors and prints the commands for rerunning the changed-files or full scan. Use `git push --no-verify` only as an explicit emergency bypass; CI still runs the full gate.

An uncached `pnpm dlx` run requires GitHub access to retrieve that pinned commit. Cached runs preserve the same SHA-based command and do not resolve a moving release tag.
