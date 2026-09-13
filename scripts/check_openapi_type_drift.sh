#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${AGENTGOV_PYTHON:-${repo_root}/.venv/bin/python}"
temporary_dir="$(mktemp -d)"

cleanup() {
  rm -rf -- "${temporary_dir}"
}
trap cleanup EXIT

"${python_bin}" "${repo_root}/scripts/export_openapi.py" \
  --output "${temporary_dir}/openapi.json"
"${python_bin}" "${repo_root}/scripts/agentscope_native_contract.py" --check
(
  cd "${repo_root}/frontend"
  pnpm exec openapi-typescript \
    "${temporary_dir}/openapi.json" \
    -o "${temporary_dir}/api.ts"
  pnpm exec openapi-typescript \
    --default-non-nullable false \
    "${repo_root}/config/agentscope_openapi.json" \
    -o "${temporary_dir}/agentscope.ts"
)

if ! cmp -s "${repo_root}/frontend/src/types/api.ts" "${temporary_dir}/api.ts"; then
  echo "OPENAPI_TYPE_DRIFT: frontend/src/types/api.ts is stale; run pnpm --dir frontend generate:api-types" >&2
  diff --unified "${repo_root}/frontend/src/types/api.ts" "${temporary_dir}/api.ts" || true
  exit 1
fi

if ! cmp -s "${repo_root}/frontend/src/types/agentscope.ts" "${temporary_dir}/agentscope.ts"; then
  echo "OPENAPI_TYPE_DRIFT: frontend/src/types/agentscope.ts is stale; run pnpm --dir frontend generate:agentscope-types" >&2
  diff --unified "${repo_root}/frontend/src/types/agentscope.ts" "${temporary_dir}/agentscope.ts" || true
  exit 1
fi

echo "OPENAPI_TYPE_DRIFT_OK: AgentGov and AgentScope generated types match their fixed schemas"
