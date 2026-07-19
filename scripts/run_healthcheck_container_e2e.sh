#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

project="agentgov-health-e2e-$$"
prefix="$project"
runtime_root=$(mktemp -d /tmp/agentgov-health-runtime.XXXXXX)
artifact_root=${VERIFY_SCREENSHOT_DIR:-$(mktemp -d /tmp/agentgov-health-artifacts.XXXXXX)}
api_key=health-e2e-key
compose_env_file=${COMPOSE_ENV_FILE:-}
generated_env_file=
if [[ -z "$compose_env_file" ]]; then
  if [[ -f docker/.env ]]; then
    compose_env_file=docker/.env
  else
    generated_env_file=$(mktemp /tmp/agentgov-compose-env.XXXXXX)
    cp docker/.env.example "$generated_env_file"
    compose_env_file=$generated_env_file
  fi
fi
if [[ ! -f "$compose_env_file" ]]; then
  echo "Compose env file does not exist: $compose_env_file" >&2
  exit 1
fi
compose_env_file=$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$compose_env_file")
export COMPOSE_ENV_FILE="$compose_env_file"
export AGENT_GOV_COMPOSE_ENV_FILE="$compose_env_file"

free_port() {
  python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

api_port=$(free_port)
ui_port=$(free_port)
compose=(
  docker compose
  --env-file "$compose_env_file"
  -f docker/docker-compose.yml
  -f docker/e2e/docker-compose.provider-health.yml
  --project-name "$project"
)

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  if [[ -d "$runtime_root" ]]; then
    docker run --rm --network none \
      --volume "$runtime_root:/runtime" \
      --entrypoint sh agent-gov-api:dev \
      -c 'chmod -R a+rwX /runtime' >/dev/null 2>&1 || true
    rm -rf "$runtime_root" || true
  fi
  [[ -z "$generated_env_file" ]] || rm -f "$generated_env_file"
}
trap cleanup EXIT

export CONTAINER_NAME_PREFIX="$prefix"
export HOST_PORT="$api_port"
export FRONTEND_HOST_PORT="$ui_port"
export FRONTEND_RUNTIME_API_BASE="http://localhost:$api_port"
export FRONTEND_RUNTIME_API_KEY="$api_key"
export API_KEY="$api_key"
export HOST_RUNTIME_VOLUME_ROOT="$runtime_root"
export HOST_DATA_MOUNT="$runtime_root/data"
export HOST_GOVERNOR_WORKSPACE_MOUNT="$runtime_root/governor-workspace"
export HOST_GOVERNOR_CLAUDE_ROOT_MOUNT="$runtime_root/claude-roots/governor"
export RUNTIME_BOOTSTRAP_HOST_DIR="$ROOT_DIR/docker/runtime-bootstrap"

mkdir -p "$HOST_DATA_MOUNT" "$HOST_GOVERNOR_WORKSPACE_MOUNT" "$HOST_GOVERNOR_CLAUDE_ROOT_MOUNT" "$artifact_root"

"${compose[@]}" build slow-vllm agent-gov-litellm-sidecar claude-agent-api claude-agent-ui
services=$("${compose[@]}" config --services)
grep -qx "slow-vllm" <<<"$services"
if grep -q "claude-agent-worker" <<<"$services"; then
  echo "retired claude-agent-worker is still present in the E2E stack" >&2
  exit 1
fi

started_at=$(date +%s)
if ! "${compose[@]}" up -d --wait --wait-timeout 90 --remove-orphans; then
  "${compose[@]}" ps --all || true
  "${compose[@]}" logs --no-color --tail 120 claude-agent-api agent-gov-litellm-sidecar slow-vllm || true
  exit 1
fi
startup_seconds=$(( $(date +%s) - started_at ))
echo "Compose control plane startup completed in ${startup_seconds}s"

diagnosis=$(
  .venv/bin/python scripts/diagnose_runtime_health.py \
    --api-base "http://localhost:$api_port" \
    --wait-seconds 10
)
printf '%s\n' "$diagnosis"
grep -Fq "API: healthy" <<<"$diagnosis"
grep -Fq "Model provider: degraded" <<<"$diagnosis"
grep -Fq "error_code=VLLM_VERSION_PROBE_FAILED" <<<"$diagnosis"
grep -Fq "reason=timeout" <<<"$diagnosis"
grep -Fq "根因: API 容器已存活；外部模型 provider 就绪探测失败" <<<"$diagnosis"
grep -Fq "这不是镜像启动失败，Compose dependency 报错只是次级症状" <<<"$diagnosis"

RUNTIME_UI_BASE="http://localhost:$ui_port" \
RUNTIME_API_BASE="http://localhost:$api_port" \
RUNTIME_API_KEY="$api_key" \
VERIFY_SCREENSHOT_DIR="$artifact_root" \
pnpm --dir frontend run verify:provider-health-container

log_file="$artifact_root/container.log"
"${compose[@]}" logs --no-color claude-agent-api agent-gov-litellm-sidecar slow-vllm >"$log_file" 2>&1
if grep -Fq "$api_key" "$log_file"; then
  echo "container logs leaked the E2E API key" >&2
  exit 1
fi

echo "PROVIDER_HEALTH_CONTAINER_E2E passed startup_seconds=$startup_seconds screenshots=$artifact_root"
