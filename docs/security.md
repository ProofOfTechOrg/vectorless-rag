<!-- Content type: Conceptual -->

# Understand the security model

This page explains the browser session boundary, FastAPI authorization, untrusted corpus controls, and deployment requirements.

## Browser and BFF

The browser-facing frontend (BFF) keeps API keys out of client-readable state and authorizes protected calls at the server boundary.

The browser calls only the TanStack Start origin. Login sends the password-style key to a server function, which validates it against FastAPI `GET /v1/auth/me`; FastAPI returns only `{name, scopes}`. The BFF encrypts key, identity, scopes, and expiry with AES-256-GCM in an HttpOnly eight-hour cookie.

Cookie flags are `SameSite=Lax` and `Path=/`; production adds `Secure` and the `__Host-` prefix. The session is rejected on authentication-tag failure or expiry. Upstream `401` clears it. Appearance is the only local-storage value.

All mutation endpoints require an `Origin` whose parsed origin exactly equals `APP_ORIGIN`. TanStack cross-site request forgery (CSRF) middleware protects server functions. Explicit checks protect upload and chat server routes.

Every protected BFF endpoint reads the session, and every protected FastAPI endpoint re-authorizes the scope. Public or session-establishing exceptions are health checks, the connection function, and session introspection. Route guards and disabled buttons are only user experience controls.

Keys must never appear in client environment variables, URLs, logs, SSR payloads, browser storage, screenshots, traces, video, Playwright storage state, committed fixtures, or documentation. Browser calls must not carry `X-API-Key`; only the server-side API client adds it.

## API and corpus

FastAPI stores verifiers instead of raw API keys and constrains both generated queries and ingestion inputs.

FastAPI stores only an API-key prefix and HMAC-SHA256 digest. Keys have `chat`, `documents:read`, and/or `admin:ingest`. Thread history/listing and feedback are isolated by API-key ID. Document content is treated as untrusted data rather than instructions.

SQL generation is restricted to a single read-only `SELECT`/CTE over curated views and executes with row, statement-timeout, lock-timeout, payload, and explain-cost bounds. Chat has a request deadline and streaming disconnects propagate cancellation.

Uploads accept bounded born-digital PDFs. Mounted sync uses a fixed configured root and direct `.pdf` children, never a caller-provided path. The 25-document registration gate, disabled full-corpus flag, frozen manifest, and database-enforced $10 reservation ceiling prevent accidental corpus-wide model use.

Provider state is categorical: `available`, `unavailable`, `unknown`, or `unconfigured`. The application never persists balance amounts, response bodies, headers, endpoint URLs, or credentials. HTTP 402, 401, and 403 open a hard circuit and persist categorical unavailability across worker replicas. The gate probes again after its status cache expires and closes the circuit only after a fresh affirmative balance response. Retryable network, 429, and 5xx failures retain bounded inner retries and consume one outer retry-budget unit after those retries fail. A successful balance check cannot resume that work until the provider retry cooldown expires. `/health/ready` remains an infrastructure check so provider credit cannot restart healthy non-model services.

PageIndex child processes receive an allowlisted environment. Database admin/reader credentials, object-store credentials, API-key pepper, frontend secrets, and unrelated host variables are excluded. Provider failures store allowlisted codes, status, retryability, and stack fingerprints, never messages, prompts, document text, response bodies, or locals.

## Deployment checklist

Use this checklist before exposing either the frontend or FastAPI.

- Terminate HTTPS before the frontend; set exact `APP_ORIGIN` and a unique 32-byte `SESSION_SECRET`.
- Rotate the session secret as a deliberate global logout. Do not casually change `API_KEY_PEPPER`, which would invalidate API keys.
- Keep FastAPI private when the browser console is the intended entry point; if API ingress stays enabled, protect and monitor it separately.
- Compose publishes FastAPI only on host loopback at `127.0.0.1:8000`; the frontend still reaches it privately as `http://api:8000` over the Compose network.
- Use unique database, object store, Redis, Langfuse, and model credentials.
- Disable Playwright credential artifacts and use a dedicated disposable development key for live verification.
- Review proxy logs and error handling to ensure request headers and cookies are redacted.
