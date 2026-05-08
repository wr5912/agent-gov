#!/usr/bin/env bash
set -euo pipefail
API_BASE=${API_BASE:-http://localhost:8080}
API_KEY=${API_KEY:-change-me}

curl -s "$API_BASE/health" | python -m json.tool
curl -s -H "Authorization: Bearer $API_KEY" "$API_BASE/api/agents" | python -m json.tool
curl -s -H "Authorization: Bearer $API_KEY" "$API_BASE/api/skills" | python -m json.tool
