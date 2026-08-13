#!/usr/bin/env bash
set -euo pipefail

case "${BASH_SOURCE[0]}" in
  /*) SCRIPT_PATH=${BASH_SOURCE[0]} ;;
  *) SCRIPT_PATH=$PWD/${BASH_SOURCE[0]} ;;
esac
ROOT_DIR=${SCRIPT_PATH%/scripts/run_healthcheck_container_e2e.sh}
if [[ "$ROOT_DIR" == "$SCRIPT_PATH" || ! -d "$ROOT_DIR/scripts" ]]; then
  echo "Managed isolated-health verifier source root is invalid." >&2
  exit 1
fi
cd "$ROOT_DIR"

required=(
  AGENT_GOV_ACCEPTANCE_DOCKER
  AGENT_GOV_ACCEPTANCE_MAKE
  AGENT_GOV_ACCEPTANCE_PNPM
  AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY
  AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY_SHA256
  AGENT_GOV_ACCEPTANCE_RUN_ID
  AGENT_GOV_COMPOSE_ENV_FILE
  API_BASE
  API_KEY
  COMPOSE_PROJECT_NAME
  FRONTEND_HOST_PORT
  VERIFY_SCREENSHOT_DIR
)
for key in "${required[@]}"; do
  if [[ -z "${!key:-}" ]]; then
    echo "Managed isolated-health verifier environment is incomplete." >&2
    exit 1
  fi
done
if [[ "${AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE:-}" != "1" || "${AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE:-}" != "isolated-health" ]]; then
  echo "Use make container-health-e2e so the isolated stack is managed by the public runner." >&2
  exit 1
fi
if [[ ! -d "$VERIFY_SCREENSHOT_DIR" ]]; then
  echo "Managed isolated-health artifact scope is unavailable." >&2
  exit 1
fi

if ! diagnosis=$(
  "$AGENT_GOV_ACCEPTANCE_MAKE" --no-print-directory \
    -f "$ROOT_DIR/Makefile" _container-health-diagnose 2>/dev/null
); then
  echo "failure_phase=provider_health_diagnose failure_code=ChildExit" >&2
  exit 1
fi
if [[ "$diagnosis" != *"API: healthy"* ]] \
  || [[ "$diagnosis" != *"Model provider: degraded"* ]] \
  || [[ "$diagnosis" != *"error_code=VLLM_VERSION_PROBE_FAILED"* ]] \
  || [[ "$diagnosis" != *"reason=timeout"* ]] \
  || [[ "$diagnosis" != *"根因: API 容器已存活；外部模型 provider 就绪探测失败"* ]] \
  || [[ "$diagnosis" != *"这不是镜像启动失败，Compose dependency 报错只是次级症状"* ]]; then
  echo "failure_phase=provider_health_diagnose failure_code=ContractMismatch" >&2
  exit 1
fi

if ! RUNTIME_UI_BASE="http://localhost:$FRONTEND_HOST_PORT" \
  RUNTIME_API_BASE="$API_BASE" \
  RUNTIME_API_KEY="$API_KEY" \
  VERIFY_SCREENSHOT_DIR="$VERIFY_SCREENSHOT_DIR" \
  "$AGENT_GOV_ACCEPTANCE_PNPM" --silent --dir frontend run verify:provider-health-container:impl \
  >/dev/null 2>&1; then
  echo "failure_phase=provider_health_browser failure_code=ChildExit" >&2
  exit 1
fi

compose=(
  "$AGENT_GOV_ACCEPTANCE_DOCKER" compose
  --parallel 3
  --env-file "$AGENT_GOV_COMPOSE_ENV_FILE"
  -f "$ROOT_DIR/docker/docker-compose.yml"
  -f "$ROOT_DIR/docker/e2e/docker-compose.provider-health.yml"
  --project-name "$COMPOSE_PROJECT_NAME"
)
log_file="$VERIFY_SCREENSHOT_DIR/container.log"
"${compose[@]}" logs --no-color claude-agent-api agent-gov-litellm-sidecar slow-vllm >"$log_file" 2>&1
if [[ "$(<"$log_file")" == *"$API_KEY"* ]]; then
  echo "Container logs leaked the managed E2E API key." >&2
  exit 1
fi

echo "PROVIDER_HEALTH_CONTAINER_E2E_OK"
