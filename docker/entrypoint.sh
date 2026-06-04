#!/usr/bin/env sh
set -eu

relax_volume_permissions() {
    path="$1"
    if [ -e "$path" ]; then
        chmod -R a+rwX "$path" 2>/dev/null || true
    fi
}

ensure_claude_config_dir() {
    root="$1"
    mkdir -p "$root/.claude"
}

if [ -d /app/docker/runtime-template ] && [ -f /app/scripts/bootstrap_runtime_volume.py ]; then
    python /app/scripts/bootstrap_runtime_volume.py \
        --runtime-root / \
        --template-dir /app/docker/runtime-template \
        --quiet
fi

ensure_claude_config_dir "${MAIN_CLAUDE_ROOT:-${CLAUDE_ROOT:-/claude-roots/main}}"
ensure_claude_config_dir "${ATTRIBUTION_ANALYZER_CLAUDE_ROOT:-/claude-roots/attribution-analyzer}"
ensure_claude_config_dir "${PROPOSAL_GENERATOR_CLAUDE_ROOT:-/claude-roots/proposal-generator}"
ensure_claude_config_dir "${EXECUTION_OPTIMIZER_CLAUDE_ROOT:-/claude-roots/execution-optimizer}"
ensure_claude_config_dir "${EVAL_CASE_GOVERNOR_CLAUDE_ROOT:-/claude-roots/eval-case-governor}"
ensure_claude_config_dir "${REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT:-/claude-roots/regression-impact-analyzer}"

relax_volume_permissions "${MAIN_WORKSPACE_DIR:-${WORKSPACE_DIR:-/main-workspace}}"
relax_volume_permissions "${ATTRIBUTION_ANALYZER_WORKSPACE_DIR:-/attribution-analyzer-workspace}"
relax_volume_permissions "${PROPOSAL_GENERATOR_WORKSPACE_DIR:-/proposal-generator-workspace}"
relax_volume_permissions "${EXECUTION_OPTIMIZER_WORKSPACE_DIR:-/execution-optimizer-workspace}"
relax_volume_permissions "${EVAL_CASE_GOVERNOR_WORKSPACE_DIR:-/eval-case-governor-workspace}"
relax_volume_permissions "${REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR:-/regression-impact-analyzer-workspace}"
relax_volume_permissions "${DATA_DIR:-/data}"
relax_volume_permissions "${MAIN_CLAUDE_ROOT:-${CLAUDE_ROOT:-/claude-roots/main}}"
relax_volume_permissions "${ATTRIBUTION_ANALYZER_CLAUDE_ROOT:-/claude-roots/attribution-analyzer}"
relax_volume_permissions "${PROPOSAL_GENERATOR_CLAUDE_ROOT:-/claude-roots/proposal-generator}"
relax_volume_permissions "${EXECUTION_OPTIMIZER_CLAUDE_ROOT:-/claude-roots/execution-optimizer}"
relax_volume_permissions "${EVAL_CASE_GOVERNOR_CLAUDE_ROOT:-/claude-roots/eval-case-governor}"
relax_volume_permissions "${REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT:-/claude-roots/regression-impact-analyzer}"

exec "$@"
