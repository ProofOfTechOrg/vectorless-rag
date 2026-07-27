<!-- Content type: Reference -->
<!-- Content plan: Goal: route vulnerability reports privately and define supported versions. Audience: security researchers, operators, and maintainers. Content: support window, reporting channel, response expectations, and secret handling. Open questions: none. -->

# Report a Vectorless RAG security vulnerability

Use this policy for exploitable defects in Vectorless RAG. Do not disclose sensitive vulnerabilities in public issues, Discussions, or pull requests.

## Supported versions

Security fixes target these versions:

| Version | Supported |
| --- | --- |
| Current `master` | Yes |
| 0.2.x research previews | Yes |
| Earlier versions | No |

Support means the maintainers will assess a report and may prepare a fix. This research preview has no guaranteed response or remediation service-level agreement.

## Send a private report

Open a [private GitHub security advisory](https://github.com/ProofOfTechOrg/vectorless-rag/security/advisories/new). Include the affected version or commit, impact, prerequisites, reproduction steps, and a minimal proof of concept.

Remove real API keys, provider prompts, private PDF text, and credentials from the report. Use synthetic data whenever possible.

If GitHub private reporting is unavailable, email [hello@proofoftech.org](mailto:hello@proofoftech.org) with a request for a private reporting channel. Do not send exploit details until the recipient confirms the channel.

## Protect secrets during research

Never place a Vectorless RAG key or deployment secret in:

- `VITE_*` variables, URLs, logs, or committed files
- Browser local storage, session storage, or server-side rendering data
- Screenshots, traces, videos, Playwright storage state, or fixtures
- Public issues, Discussions, pull requests, or test output

Do not attach private source PDFs, model prompts, or provider response bodies. The [security model](docs/security.md) documents the application’s trust boundaries and deployment controls.
