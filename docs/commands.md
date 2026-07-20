<!-- Content type: Reference -->

# Command reference

This page lists every public Make target and the variables that alter operator commands. Run all commands from the repository root. `make help` generates its target list from the Makefile.

| Area | Commands |
| --- | --- |
| Discovery and environment | `make help`, `make env` |
| Dependencies | `make install`, `make api-install`, `make web-install` |
| Formatting | `make format`, `make api-format`, `make web-format` |
| Linting | `make lint`, `make api-lint`, `make web-lint` |
| Type checking | `make typecheck`, `make api-typecheck`, `make web-typecheck` |
| Unit and integration tests | `make test`, `make api-test`, `make web-test`, `make test-unit`, `make test-integration` |
| Package builds | `make api-build`, `make web-build` |
| Browser tests | `make playwright-install`, `make playwright`, `make playwright-live PLAYWRIGHT_BASE_URL=http://127.0.0.1:3001` |
| Web diagnostics | `make react-doctor`, `make react-doctor-diff`, `make astryx-doctor` |
| Manifest validation | `make workflow-check`, `make migration-check`, `make compose-check`, `make helm-lint`, `make helm-template`, `make helm-check` |
| Image builds | `make docker-build-api`, `make docker-build-web`, `make docker-build` |
| Validation suites | `make api-check`, `make web-check`, `make deploy-check`, `make check ENV_FILE=.env.example`, `make ci` |
| Lifecycle | `make up`, `make down`, `make stop`, `make restart`, `make restart-app`, `make restart-web`, `make restart-langfuse`, `make restart-observability`, `make ps`, `make logs SERVICE=worker`, `make health`, `make web-dev` |
| Corpus | `make download-corpus`, `make download-corpus ARXIV_PDF_HOST_DIR=/srv/vectorless/arxiv-pdfs` |
| Operator | `make migrate`, `make bootstrap-key NAME=operator`, `make sync-corpus`, `make upload-document PDF=/path/file.pdf`, `make pilot-report` |
| Evaluation | `make evaluation-build`, `make evaluation-seed DATASET=apps/api/evaluation/release-v1.jsonl` |
| Secret generation | `make secret-api-pepper`, `make secret-session`, `make secrets-langfuse` |

`make playwright-live` exits with status 2 unless both `PLAYWRIGHT_BASE_URL` and the process-only `PLAYWRIGHT_API_KEY` are set. Use the [disposable live-stack procedure](operator-guide.md#verify-a-disposable-live-stack) to create both values without saving the key.

`make ci` installs Playwright Chromium and its operating-system dependencies through `make playwright-install` before it runs browser tests.

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

The default ten-document pilot gate still applies. Do not enable full-corpus indexing until you have reviewed pilot quality and projected provider cost.

`make web-dev` runs the Start development server on port 3001 against FastAPI on port 8000. It still needs `SESSION_SECRET`; load it from your shell or `.env` without using a `VITE_` name.

Operator curl targets require `VECTORLESS_RAG_ADMIN_KEY` in the process environment. They never read the browser session. `make down` preserves named volumes; do not use `docker compose down -v` unless a full local data reset is intended.

## React Doctor

These commands run the full-project gate or a changed-files diagnostic against the immutable React Doctor Git commit configured in `apps/web/package.json`.

`make react-doctor` (or `pnpm react-doctor`) runs the authoritative full-project React Doctor scan. CI and `make web-check` require this scan to remain at 100/100 with warnings treated as blocking findings.

`make react-doctor-diff` (or `pnpm react-doctor:diff`) scans only files changed from the current branch's open pull request base. The resolver prefers `origin/<base>`, falls back to a local base branch, and otherwise uses React Doctor's `parent` fork-point heuristic. It does not fetch or mutate remote refs.

Husky runs the changed-files scan before every push. The hook fails closed on findings or tool errors and prints the commands for rerunning the changed-files or full scan. Use `git push --no-verify` only as an explicit emergency bypass; CI still runs the full gate.

An uncached `pnpm dlx` run requires GitHub access to retrieve that pinned commit. Cached runs preserve the same SHA-based command and do not resolve a moving release tag.
