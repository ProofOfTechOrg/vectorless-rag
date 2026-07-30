#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker_bin="${DOCKER:-docker}"
uv_bin="${UV:-uv}"
make_bin="${MAKE:-make}"
postgres_port="${TEST_POSTGRES_PORT:-55432}"
project_name="vectorless-rag-integration-$PPID-$$-$RANDOM"
compose_args=(
  compose
  --project-name "$project_name"
  --env-file "$root_dir/.env.example"
  -f "$root_dir/compose.yaml"
)

if [[ ! "$postgres_port" =~ ^[0-9]+$ ]] || ((postgres_port < 1 || postgres_port > 65535)); then
  echo "TEST_POSTGRES_PORT must be an integer from 1 through 65535" >&2
  exit 2
fi

cleanup() {
  POSTGRES_PORT="$postgres_port" "$docker_bin" "${compose_args[@]}" \
    down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "starting disposable PostgreSQL project on port $postgres_port"
POSTGRES_PORT="$postgres_port" "$docker_bin" "${compose_args[@]}" up -d --wait postgres

owner_url="postgresql+psycopg://rag_owner:rag-owner-change-me@127.0.0.1:$postgres_port/rag"
app_url="postgresql+psycopg://rag_app:rag-app-change-me@127.0.0.1:$postgres_port/rag"
reader_url="postgresql+psycopg://rag_reader:rag-reader-change-me@127.0.0.1:$postgres_port/rag"

(
  cd "$root_dir/apps/api"
  DATABASE_ADMIN_URL="$owner_url" "$uv_bin" run vectorless-rag migrate
)
(
  cd "$root_dir"
  TEST_OWNER_DATABASE_URL="$owner_url" TEST_APP_DATABASE_URL="$app_url" \
    TEST_DATABASE_URL="$reader_url" \
    "$make_bin" test-integration
)
