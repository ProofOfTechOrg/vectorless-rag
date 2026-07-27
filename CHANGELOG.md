<!-- Content type: Reference -->
<!-- Content plan: Goal: record user-visible release changes and research-preview limits. Audience: users, operators, and maintainers. Content: unreleased changes and dated releases. Open questions: none. -->

# Track Vectorless RAG releases

This changelog records user-visible changes. Vectorless RAG follows semantic version labels, but prerelease status does not promise production stability.

## Unreleased

No user-visible changes have been recorded after v0.2.0.

## 0.2.0, 2026-07-27

Version 0.2.0 publishes the repository under the MIT License as a source-only research preview.

### Added

- TanStack Start operator console for chat, documents, ingestion, settings, scoped connection, appearance, and logout
- Encrypted HttpOnly browser session backed by FastAPI API-key validation
- Durable PDF ingestion, PageIndex v2 artifacts, catalog routing, page citations, usage accounting, and provider-state handling
- Docker Compose and Helm deployment surfaces
- Deterministic API, PostgreSQL integration, browser, deployment, image, and evaluation checks
- Atomic local environment initialization and indexed public documentation

### Research-preview limitations

- The default gate permits 25 registered documents and keeps full-corpus ingestion disabled
- The frozen pilot has a $10 provider-cost ceiling
- DeepSeek-backed ingestion and chat incur external provider cost
- Optical character recognition, API-key lifecycle management, and an embedded PDF viewer are unavailable
- Full 537-document ingestion and a final vector-versus-PageIndex quality verdict remain outside this release
- The release contains source archives only
