# Security model

## Browser and BFF

The browser calls only the TanStack Start origin. Login sends the password-style key to a server function, which validates it against FastAPI `GET /v1/auth/me`; FastAPI returns only `{name, scopes}`. The BFF encrypts key, identity, scopes, and expiry with AES-256-GCM in an HttpOnly eight-hour cookie.

Cookie flags are `SameSite=Lax` and `Path=/`; production adds `Secure` and the `__Host-` prefix. The session is rejected on authentication-tag failure or expiry. Upstream `401` clears it. Appearance is the only local-storage value.

All mutation endpoints require an `Origin` whose parsed origin exactly equals `APP_ORIGIN`. TanStack CSRF middleware protects server functions and explicit checks protect upload/chat server routes. Every BFF endpoint reads the session and every FastAPI endpoint re-authorizes the scope; route guards and disabled buttons are only UX.

Keys must never appear in client environment variables, URLs, logs, SSR payloads, browser storage, screenshots, traces, video, Playwright storage state, committed fixtures, or documentation. Browser calls must not carry `X-API-Key`; only the server-side API client adds it.

## API and corpus

FastAPI stores only an API-key prefix and HMAC-SHA256 digest. Keys have `chat`, `documents:read`, and/or `admin:ingest`. Thread history/listing and feedback are isolated by API-key ID. Document content is treated as untrusted data rather than instructions.

SQL generation is restricted to a single read-only `SELECT`/CTE over curated views and executes with row, statement-timeout, lock-timeout, payload, and explain-cost bounds. Chat has a request deadline and streaming disconnects propagate cancellation.

Uploads accept bounded born-digital PDFs. Mounted sync uses a fixed configured root and direct `.pdf` children, never a caller-provided path. The ten-document pilot gate prevents accidental corpus-wide model use.

## Deployment checklist

- Terminate HTTPS before the frontend; set exact `APP_ORIGIN` and a unique 32-byte `SESSION_SECRET`.
- Rotate the session secret as a deliberate global logout. Do not casually change `API_KEY_PEPPER`, which would invalidate API keys.
- Keep FastAPI private when the browser console is the intended entry point; if API ingress stays enabled, protect and monitor it separately.
- Use unique database, object store, Redis, Langfuse, and model credentials.
- Disable Playwright credential artifacts and use a dedicated disposable development key for live verification.
- Review proxy logs and error handling to ensure request headers and cookies are redacted.
