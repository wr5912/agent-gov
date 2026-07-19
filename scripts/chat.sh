#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

env_value() {
  local key=$1
  [[ -f "$ROOT_DIR/docker/.env" ]] || return 0
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ROOT_DIR/docker/.env"
}

HOST_PORT=${HOST_PORT:-$(env_value HOST_PORT)}
API_BASE=${API_BASE:-$(env_value API_BASE)}
API_BASE=${API_BASE:-http://localhost:${HOST_PORT:-58080}}
API_KEY=${API_KEY:-$(env_value API_KEY)}
API_KEY=${API_KEY:-change-me}
MESSAGE=${1:-"你好，请说明你当前可用的 agents 和 skills。"}
PYTHON=${PYTHON:-$ROOT_DIR/.venv/bin/python}

"$PYTHON" - <<'PY' "$API_BASE" "$API_KEY" "$MESSAGE"
import json
import sys
import urllib.request

api_base, api_key, message = sys.argv[1:4]
payload = json.dumps({"message": message, "agent_id": "security-operations-expert"}).encode("utf-8")
req = urllib.request.Request(
    api_base + "/api/chat",
    data=payload,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    print(json.dumps(json.loads(resp.read().decode("utf-8")), ensure_ascii=False, indent=2))
PY
