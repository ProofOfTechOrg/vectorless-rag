<!-- Content type: Reference -->
<!-- Content plan: Goal: select and run the correct verification tier without accidental provider spend. Audience: contributors, release managers, and operators. Content: unit, PostgreSQL, browser, deployment, image, evaluation, CI, and live tiers. Open questions: none. -->

# Choose a test tier

This reference maps each verification command to its dependencies, provider use, and completion signal. Continuous integration (CI) runs only provider-free tiers.

## Install frozen dependencies

Run the repository install gate once:

```bash
make install
```

The pnpm and uv configurations delay newly published dependencies for seven days. Both package managers install from committed lockfiles.

## Run provider-free tiers

Use these tiers during development:

| Tier | Command | External requirements | Done when |
| --- | --- | --- | --- |
| Documentation | `make docs-check` | None after install | Community files, links, anchors, declarations, fences, and index pass |
| API | `make api-check` | None | Lint, type checks, tests, and offline migrations pass |
| PostgreSQL | `make test-integration-stack` | Docker | All integration tests run against owner, application, and reader roles, then the isolated volume is removed |
| Web | `make web-check` | None | Lint, type checks, unit tests, React Doctor, production smoke, and ASTRYX Doctor pass |
| Browser | `make playwright-install && make playwright` | Chromium installation | Deterministic browser tests pass against the mock upstream |
| Deployment | `make deploy-check ENV_FILE=.env.example` | Docker and Helm 4.2.3 | Workflow, Compose, and Helm validation pass |
| Images | `make docker-build` | Docker | API and web production images build |

Override the isolated PostgreSQL port when `55432` is occupied:

```bash
TEST_POSTGRES_PORT=55433 make test-integration-stack
```

The runner creates a unique Compose project. Its cleanup trap addresses only that project and its volumes.

## Run deterministic evaluation checks

The PageIndex evaluation schema and review-pack generator need no PDF, database, model, or Langfuse:

```bash
make evaluation-check
make evaluation-review-pack
```

The generated review pack supports human source review. It does not produce a model-quality score.

## Run the local CI equivalent

Run every provider-free gate, browser suite, integration stack, and image build:

```bash
make ci
```

The command uses `.env.example` only for manifest rendering. It does not start the normal Compose project or read `DEEPSEEK_API_KEY`.

## Authorize paid live verification separately

`make playwright-live` uses a real DeepSeek-backed stack and can incur cost. Run it only for an explicitly authorized release candidate.

Follow [verify a disposable live stack](operator-guide.md#verify-a-disposable-live-stack). The release gate must cover upload, durable ingestion, Server-Sent Events (SSE) completion, document and page citation, thread history, feedback, document detail, identity and scopes, theme, responsive navigation, and logout.

Stop the release if ingestion or chat fails. Never add provider credentials to CI.
