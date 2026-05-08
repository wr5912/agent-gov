#!/usr/bin/env bash
set -euo pipefail
API_BASE=${API_BASE:-http://localhost:8080}
API_KEY=${API_KEY:-change-me}
MESSAGE=${1:-"你好，请说明你当前可用的 agents 和 skills。"}

python - <<'PY' "$API_BASE" "$API_KEY" "$MESSAGE"
import json
import sys
import urllib.request

api_base, api_key, message = sys.argv[1:4]
payload = json.dumps({"message": message, "skills_mode": "all"}).encode("utf-8")
req = urllib.request.Request(
    api_base + "/api/chat",
    data=payload,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    print(json.dumps(json.loads(resp.read().decode("utf-8")), ensure_ascii=False, indent=2))
PY
