#!/usr/bin/env sh
# Resolve the react-doctor --base ref for the current branch's changed-file scan.
# Print exactly one machine-readable token to stdout; diagnostics go to stderr.
set -eu

fallback=parent

emit() {
	printf '%s\n' "$1"
	printf 'react-doctor diff base: %s (%s)\n' "$1" "$2" >&2
	exit 0
}

if ! command -v gh >/dev/null 2>&1; then
	emit "$fallback" "gh CLI not found"
fi

timeout_cmd=""
if command -v timeout >/dev/null 2>&1; then
	timeout_cmd="timeout 5"
fi

base=$(${timeout_cmd} gh pr view --json state,baseRefName --jq 'select(.state == "OPEN").baseRefName' 2>/dev/null || true)

[ -z "$base" ] && emit "$fallback" "no open PR base resolved via gh"

case "$base" in
	*[!A-Za-z0-9_./-]* | *..*) emit "$fallback" "PR base '$base' is not a safe ref" ;;
esac

if git rev-parse --verify "refs/remotes/origin/$base" >/dev/null 2>&1; then
	emit "origin/$base" "PR base (remote-tracking)"
elif git rev-parse --verify "refs/heads/$base" >/dev/null 2>&1; then
	emit "$base" "PR base (local)"
else
	emit "$fallback" "PR base '$base' not found in origin/ or heads/"
fi
