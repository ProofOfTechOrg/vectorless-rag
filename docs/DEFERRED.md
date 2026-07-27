<!-- Content type: Reference -->
<!-- Content plan: Goal: preserve accepted hardening work excluded from the current release. Audience: maintainers and contributors. Content: deferred type-checking scope and completion gate. Open questions: scheduling. -->

# Deferred hardening

This page records accepted hardening work that remains outside the current implementation scope.

## Adopt Basedpyright recommended mode

Move from Basedpyright’s `strict` mode to its full `recommended` mode. Resolve every additional warning and error directly instead of adding a baseline or broad rule suppression. Cover explicit and propagated `Any`, unannotated class attributes, unused call results and parameters, implicit string concatenation, and Basedpyright-specific safety rules.

Completion requires `make api-typecheck` to finish with zero errors and zero warnings.
