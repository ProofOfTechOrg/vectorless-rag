<!-- intent-skills:start -->
## TanStack Intent skill loading

Before substantial frontend edits, run `pnpm dlx @tanstack/intent@latest list` from `apps/web`, then load every matching local skill with `pnpm dlx @tanstack/intent@latest load <package>#<skill>`. TanStack Start is isomorphic by default: loaders run on server and client; secrets and privileged calls belong in server functions or server routes. Route guards are UX, never an authorization boundary.
<!-- intent-skills:end -->

# Repository agent guide

## Required scaffold record

The frontend was created in a fresh scratch directory with exactly:

```bash
npx @tanstack/cli@latest create my-tanstack-app --agent --package-manager pnpm --tailwind
npx @tanstack/intent@latest install
npx @tanstack/intent@latest list
```

Preserve the generated TanStack Start/Vite/Router/Vitest/Tailwind toolchain. Do not add TanStack Query, TanStack Form, third-party authentication, analytics, Turborepo, or unrelated starter integrations.

## ASTRYX workflow

ASTRYX packages must stay aligned; the current baseline is `@astryxdesign/core`, `@astryxdesign/theme-neutral`, and `@astryxdesign/cli` 0.1.6 with `@stylexjs/stylex` 0.19.x. Before changing an ASTRYX surface, run the relevant commands:

```bash
pnpm --filter @vectorless-rag/web exec astryx component --list
pnpm --filter @vectorless-rag/web exec astryx search "<pattern>"
pnpm --filter @vectorless-rag/web exec astryx component <Name> --dense
pnpm --filter @vectorless-rag/web exec astryx docs theme --dense
pnpm --filter @vectorless-rag/web exec astryx docs styling --dense
pnpm --filter @vectorless-rag/web exec astryx docs migration --dense
pnpm --filter @vectorless-rag/web exec astryx build "<what you are building>" --dense
make astryx-doctor
```

Use ASTRYX for interactive controls and product patterns; use Tailwind/custom CSS only for application layout. Mount `neutralTheme` at the root and keep appearance as `system | light | dark`. The only browser-persisted value is `vectorless:appearance`.

The global CSS layer declaration and import order is fixed:

```css
@layer reset, theme, base, astryx-base, astryx-theme, components, utilities;
@import "tailwindcss/theme.css" layer(theme);
@import "tailwindcss/preflight.css" layer(base);
@import "@astryxdesign/core/reset.css";
@import "@astryxdesign/core/astryx.css";
@import "@astryxdesign/theme-neutral/theme.css";
@import "@astryxdesign/core/tailwind-theme.css";
@import "tailwindcss/utilities.css" layer(utilities);
```

## Monorepo boundaries

- `apps/api`: Python package, Alembic, tests, evaluation, scripts, backend Dockerfile.
- `apps/web`: TanStack Start app, BFF, unit/E2E tests, frontend Dockerfile.
- `infra/docker`: database initialization.
- `infra/helm/vectorless-rag`: full-stack Kubernetes chart.
- `arxiv-pdfs`: user-owned, Git-ignored corpus; never move or modify it.
- Root: Compose, Make, pnpm workspace, environment example, docs, and CI.

Run Python commands with `uv --project apps/api` or from `apps/api`. Run JavaScript through the root pnpm workspace. Keep root commands in `Makefile` authoritative.

## Environment and deployment

Server-only frontend variables are `VECTORLESS_API_URL`, `APP_ORIGIN`, and `SESSION_SECRET`; `FRONTEND_PORT` is the Compose host port. Never prefix secrets with `VITE_`. `SESSION_SECRET` must encode exactly 32 random bytes. Compose exposes web 3001, Langfuse 3000, and FastAPI 8000. Kubernetes preserves the API ingress and has a separate optional frontend ingress.

## Security constraints

- Validate the API key only through FastAPI `GET /v1/auth/me`.
- Store it only inside an AES-256-GCM encrypted HttpOnly cookie with an eight-hour lifetime.
- Use `SameSite=Lax`, `Path=/`, and in production `Secure` plus a `__Host-` cookie name.
- Require the exact configured full `Origin` on every browser mutation.
- Authorize every server function, server route, and FastAPI endpoint independently; UI scope gating is not security.
- Never place a key in `VITE_*`, URLs, logs, local/session storage, SSR data, screenshots, traces, videos, storage state, fixtures, or committed files.
- Preserve streaming chunks and abort propagation in the chat proxy; clear the session after upstream `401`.

## Live-stack procedure

Rebuild API/worker with `make restart-app`, start web with `make restart-web`, and prefer a dedicated `codex-ui-dev` full-scope key minted inside the API container. Capture it directly into the Playwright process environment; never print or save it. Login through `/connect`; never inject cookies.

Generate one small unique text-rich PDF in a temporary directory, upload through the UI, poll the real durable job to a terminal state, then—only if ingestion succeeds—make one unique-phrase chat call and verify SSE completion, document/page citation, thread history, feedback, detail, identity/scopes, theme, responsive navigation, and logout. Remove the temporary file. Do not sync the normal 537-file mount. A real sync check requires an isolated Compose override containing only the fixture and must rely on deduplication.

Playwright traces, video, screenshots, and persisted storage state stay disabled during credentialed login.

## Known limitations and next steps

API key lifecycle management and a PDF viewer are outside the console. OCR is unsupported. Full-corpus ingestion remains pilot-gated. Existing API SSE semantics are proxied rather than redesigned. The deterministic browser suite is the default; live verification is intentionally low-volume because it can consume provider budget.
