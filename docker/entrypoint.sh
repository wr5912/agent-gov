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

if [ -d /app/docker/runtime-volume-seeds ] && [ -f /app/scripts/bootstrap_runtime_volume.py ]; then
    python /app/scripts/bootstrap_runtime_volume.py \
        --runtime-root / \
        --template-dir /app/docker/runtime-volume-seeds \
        --quiet
fi

ensure_claude_config_dir "${MAIN_CLAUDE_ROOT:-${CLAUDE_ROOT:-/claude-roots/main}}"
ensure_claude_config_dir "${GOVERNOR_CLAUDE_ROOT:-/claude-roots/governor}"

relax_volume_permissions "${MAIN_WORKSPACE_DIR:-${WORKSPACE_DIR:-/main-workspace}}"
relax_volume_permissions "${GOVERNOR_WORKSPACE_DIR:-/governor-workspace}"
relax_volume_permissions "${DATA_DIR:-/data}"
relax_volume_permissions "${MAIN_CLAUDE_ROOT:-${CLAUDE_ROOT:-/claude-roots/main}}"
relax_volume_permissions "${GOVERNOR_CLAUDE_ROOT:-/claude-roots/governor}"

exec "$@"
