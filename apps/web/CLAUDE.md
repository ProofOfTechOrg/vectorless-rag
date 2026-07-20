<!-- intent-skills:start -->
## Skill Loading

Before editing files for a substantial task:
- Run `pnpm dlx @tanstack/intent@latest list` from this package to see available local skills.
- If a listed skill matches the task, run `pnpm dlx @tanstack/intent@latest load <package>#<skill>` before changing files.
- Use the loaded guidance while making the change. Prefer the most specific skill when several match.
<!-- intent-skills:end -->

<!-- ASTRYX:START -->
Astryx v0.1.6 · 149 components. Run commands as `pnpm exec astryx <cmd>` from this directory.

Before writing UI, use `astryx build "<idea>"`, inspect the selected template with `astryx template <name> --skeleton`, and read exact props with `astryx component <Name>`. Use `astryx search`, `astryx component --list`, `astryx docs <topic>`, and `astryx doctor` instead of guessing APIs.

Import core reset and component CSS, mount `neutralTheme`, frame full pages with AppShell, use SideNav for sidebar navigation, use Table/List for dense data, use semantic status components, prefer component props, and use token-backed utilities for application layout. Keep the canonical Tailwind v4 layer order documented in the root CLAUDE.md. The user's security and product requirements take precedence over generic template guidance.
<!-- ASTRYX:END -->
