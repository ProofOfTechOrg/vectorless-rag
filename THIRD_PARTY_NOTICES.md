<!-- Content type: Reference -->
<!-- Content plan: Goal: identify bundled and direct third-party software. Audience: redistributors and operators. Content: component versions, revisions, licenses, and source links. Open questions: none. -->

# Review third-party software notices

Vectorless RAG includes or installs the following third-party software. Each component remains subject to its own license and copyright notices.

## PageIndex

The API image installs [VectifyAI PageIndex](https://github.com/VectifyAI/PageIndex) at revision `190f8b378be58199ca993566a9214dba72089c54` and applies the tracked runtime patch in `apps/api/patches/pageindex-runtime.patch`.

PageIndex is licensed under the MIT License.

## ASTRYX

The web workspace depends on these ASTRYX packages at version 0.1.6:

- `@astryxdesign/core`
- `@astryxdesign/theme-neutral`
- `@astryxdesign/cli`

The ASTRYX packages are licensed under the MIT License.

## Complete dependency records

`apps/api/uv.lock` and `pnpm-lock.yaml` record the complete dependency graphs. Review each dependency’s source distribution for its copyright and license terms.
