# Command reference

Run commands from the repository root. `make help` is generated from the Makefile.

| Area | Commands |
| --- | --- |
| Environment | `make env`, `make secret-api-pepper`, `make secret-session`, `make secrets-langfuse` |
| Dependencies | `make install`, `make api-install`, `make web-install` |
| API quality | `make api-format`, `make api-lint`, `make api-typecheck`, `make api-test`, `make api-build`, `make api-check` |
| Web quality | `make web-format`, `make web-lint`, `make web-typecheck`, `make web-test`, `make react-doctor`, `make react-doctor-diff`, `make web-build`, `make web-check`, `make astryx-doctor` |
| Browser tests | `make playwright`, `make playwright-live PLAYWRIGHT_BASE_URL=http://localhost:3001` |
| Deployment | `make compose-check`, `make helm-check`, `make docker-build-api`, `make docker-build-web`, `make docker-build` |
| Full validation | `make check ENV_FILE=.env.example`, `make ci` |
| Lifecycle | `make up`, `make down`, `make stop`, `make restart`, `make restart-app`, `make restart-web`, `make ps`, `make logs SERVICE=worker`, `make health` |
| Corpus | `make download-corpus`, `make download-corpus ARXIV_PDF_HOST_DIR=/srv/vectorless/arxiv-pdfs` |
| Operator | `make migrate`, `make bootstrap-key NAME=operator`, `make sync-corpus`, `make upload-document PDF=/path/file.pdf`, `make pilot-report` |
| Evaluation | `make evaluation-build`, `make evaluation-seed DATASET=apps/api/evaluation/release-v1.jsonl` |

## Download the test corpus

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

Downloading the corpus does not start services or queue ingestion. Start the stack, create an administrative key, and then run `make sync-corpus` when you intend to ingest the mounted PDFs.

The default ten-document pilot gate still applies. Do not enable full-corpus indexing until you have reviewed pilot quality and projected provider cost.

`make web-dev` runs the Start development server on port 3001 against FastAPI on port 8000. It still needs `SESSION_SECRET`; load it from your shell or `.env` without using a `VITE_` name.

Operator curl targets require `VECTORLESS_RAG_ADMIN_KEY` in the process environment. They never read the browser session. `make down` preserves named volumes; do not use `docker compose down -v` unless a full local data reset is intended.

## React Doctor

`make react-doctor` (or `pnpm react-doctor`) runs the authoritative full-project React Doctor scan. CI and `make web-check` require this scan to remain at 100/100 with warnings treated as blocking findings.

`make react-doctor-diff` (or `pnpm react-doctor:diff`) scans only files changed from the current branch's open pull request base. The resolver prefers `origin/<base>`, falls back to a local base branch, and otherwise uses React Doctor's `parent` fork-point heuristic. It does not fetch or mutate remote refs.

Husky runs the changed-files scan before every push. The hook fails closed on findings or tool errors and prints the commands for rerunning the changed-files or full scan. Use `git push --no-verify` only as an explicit emergency bypass; CI still runs the full gate.
