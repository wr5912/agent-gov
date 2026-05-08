#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

env_value() {
  local key=$1
  [[ -f "$ROOT_DIR/.env" ]] || return 0
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ROOT_DIR/.env"
}

HOST_PORT=${HOST_PORT:-$(env_value HOST_PORT)}
API_BASE=${API_BASE:-$(env_value API_BASE)}
API_BASE=${API_BASE:-http://localhost:${HOST_PORT:-8080}}
API_KEY=${API_KEY:-$(env_value API_KEY)}
API_KEY=${API_KEY:-change-me}
PYTHON=${PYTHON:-$ROOT_DIR/.venv/bin/python}

curl -s "$API_BASE/health" | "$PYTHON" -m json.tool
curl -s -H "Authorization: Bearer $API_KEY" "$API_BASE/api/agents" | "$PYTHON" -m json.tool
curl -s -H "Authorization: Bearer $API_KEY" "$API_BASE/api/skills" | "$PYTHON" -m json.tool
