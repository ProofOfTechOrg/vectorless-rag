<!-- Content type: Reference -->

# Deferred hardening

This page records accepted hardening work that remains outside the current implementation scope.

## Adopt Basedpyright recommended mode

Move from Basedpyright's `strict` mode to its full `recommended` mode. Resolve every
additional warning and error directly rather than adding a baseline or broad rule
suppression. The work should cover explicit and propagated `Any`, unannotated class
attributes, unused call results and parameters, implicit string concatenation, and
Basedpyright-specific safety rules.

Completion requires `make api-typecheck` to finish with zero errors and zero warnings.
