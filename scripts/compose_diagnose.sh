#!/usr/bin/env bash
set -u

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

python_bin=${AGENTGOV_OPERATION_PYTHON:-.venv/bin/python}
if [[ ! -x "$python_bin" ]]; then
  if [[ -n "${AGENTGOV_OPERATION_PYTHON:-}" ]]; then
    echo "Frozen Python interpreter is not executable" >&2
    exit 2
  fi
  python_bin=python3
fi

compose_env_file=${COMPOSE_ENV_FILE:-docker/.env}
compose_env_file=$("$python_bin" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$compose_env_file")
export COMPOSE_ENV_FILE="$compose_env_file"
export AGENT_GOV_COMPOSE_ENV_FILE="$compose_env_file"
COMPOSE=(docker compose --env-file "$compose_env_file" -f docker/docker-compose.yml)
if [[ "${COMPOSE_PROFILE:-core}" = "langfuse" ]]; then
  COMPOSE+=(-f docker/docker-compose.langfuse.yml --profile langfuse)
fi
echo "=== Compose service state ==="
"${COMPOSE[@]}" ps --all || true

api_container=$("${COMPOSE[@]}" ps -q agent-gov-api 2>/dev/null || true)
if [[ -n "$api_container" ]]; then
  echo "=== API container health ==="
  docker inspect --format 'state={{.State.Status}} exit_code={{.State.ExitCode}} error={{.State.Error}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$api_container" || true
  docker inspect --format '{{if .State.Health}}{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} output={{printf "%.500s" .Output}}{{println}}{{end}}{{end}}' "$api_container" || true
fi

echo "=== Runtime health diagnosis ==="
"$python_bin" scripts/diagnose_runtime_health.py --env-file "$compose_env_file" 2>&1 || true

echo "=== Relevant service logs ==="
"${COMPOSE[@]}" logs --no-color --tail=80 agent-gov-api agentscope-runtime agent-gov-ui 2>&1 || true
