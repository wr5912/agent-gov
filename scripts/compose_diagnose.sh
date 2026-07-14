#!/usr/bin/env bash
set -u

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

COMPOSE=(docker compose --env-file docker/.env -f docker/docker-compose.yml)
echo "=== Compose service state ==="
"${COMPOSE[@]}" ps --all || true

api_container=$("${COMPOSE[@]}" ps -q claude-agent-api 2>/dev/null || true)
if [[ -n "$api_container" ]]; then
  echo "=== API container health ==="
  docker inspect --format 'state={{.State.Status}} exit_code={{.State.ExitCode}} error={{.State.Error}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$api_container" || true
  docker inspect --format '{{if .State.Health}}{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} output={{printf "%.500s" .Output}}{{println}}{{end}}{{end}}' "$api_container" || true
fi

echo "=== Runtime health diagnosis ==="
python_bin=.venv/bin/python
[[ -x "$python_bin" ]] || python_bin=python3
"$python_bin" scripts/diagnose_runtime_health.py 2>&1 || true

echo "=== Relevant service logs ==="
"${COMPOSE[@]}" logs --no-color --tail=80 claude-agent-api agent-gov-litellm-sidecar claude-agent-ui 2>&1 || true
