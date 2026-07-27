<!-- Content type: How-to -->
<!-- Content plan: Goal: prepare a reviewable contribution that preserves project safety and verification contracts. Audience: external contributors and maintainers. Content: setup, workflow, tests, documentation, security, cost, and AI disclosure. Open questions: none. -->

# Contribute to Vectorless RAG

This guide explains how to prepare, verify, and submit a change to the research preview. Keep each pull request focused on one defect, feature, or documentation outcome.

## Prepare the development environment

You need Python 3.12, `uv`, Node.js 22, pnpm, Docker Compose 2.22 or later, GNU Make, and Helm 4.2.3.

Clone your fork and install the frozen dependency graphs:

```bash
git clone https://github.com/your_github_name/vectorless-rag.git
cd vectorless-rag
make install
```

The repository quarantines newly published dependencies for seven days through pnpm and uv configuration. Do not remove or bypass those settings to resolve an install failure.

Run `make init` only when you need a local live stack. The command refuses to replace an existing `.env`.

## Develop on a focused branch

Create a branch from current `master`:

```bash
git fetch origin
git switch master
git pull --ff-only
git switch -c fix/describe-the-defect
```

Match the surrounding code style. Fix the root cause and every code path with the same defect. Avoid unrelated formatting, new abstractions, and dependency changes.

## Verify the affected surfaces

Run the smallest relevant command while developing, then run the complete provider-free gates before opening a pull request:

```bash
make docs-check
make api-check
make test-integration-stack
make web-check
make playwright
make deploy-check ENV_FILE=.env.example
```

Changes to images or release behavior also require `make docker-build`. See [choose a test tier](docs/testing.md) for prerequisites and expected results.

Do not use DeepSeek credentials in continuous integration (CI). Do not run provider-backed tests unless you have explicit cost authorization.

## Document behavior and decisions

Update user documentation in the same pull request as the behavior change. Add a content-type and content-plan comment to every new Markdown page, use sentence-case headings, tag code fences, and add the page to `docs/README.md`.

Record user-visible changes under `Unreleased` in `CHANGELOG.md`. Preserve dated design records instead of rewriting their historical measurements with current assumptions.

## Protect credentials and research data

Never commit or post:

- API keys, session secrets, database passwords, or provider credentials
- Provider prompts, response bodies, or private document text
- Playwright traces, screenshots, videos, or storage state from credentialed tests
- Files from the Git-ignored `arxiv-pdfs` corpus

Use sanitized diagnostics in issues and pull requests. Report exploitable security defects through the [private vulnerability reporting channel](https://github.com/ProofOfTechOrg/vectorless-rag/security/advisories/new).

## Open the pull request

Push your branch and open a pull request against `master`. Complete the template with scope, verification evidence, security and provider-cost impact, and documentation changes.

Disclose AI-assisted work by naming the tool, describing its contribution, and stating how you reviewed the result. You remain responsible for every submitted line.
