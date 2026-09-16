#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "$script_dir/.." && pwd)"
compose_file="$repository_root/docker-compose.integration.yml"
project_name="uma-st2-integration-${GITHUB_RUN_ID:-local}-${BASHPID}"

compose=(
  docker compose
  --project-directory "$repository_root"
  --file "$compose_file"
  --project-name "$project_name"
)

cleanup() {
  exit_code=$?
  trap - EXIT
  "${compose[@]}" down --volumes --remove-orphans --rmi local --timeout 5 || true
  exit "$exit_code"
}

trap cleanup EXIT

"${compose[@]}" up --build --abort-on-container-exit --exit-code-from integration-test
