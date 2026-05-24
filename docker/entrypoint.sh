#!/usr/bin/env sh
set -eu

relax_volume_permissions() {
    path="$1"
    if [ -e "$path" ]; then
        chmod -R a+rwX "$path" 2>/dev/null || true
    fi
}

relax_volume_permissions "${MAIN_WORKSPACE_DIR:-${WORKSPACE_DIR:-/main-workspace}}"
relax_volume_permissions "${ATTRIBUTION_WORKSPACE_DIR:-/attribution-workspace}"
relax_volume_permissions "${PROPOSAL_WORKSPACE_DIR:-/proposal-workspace}"
relax_volume_permissions "${DATA_DIR:-/data}"
relax_volume_permissions "${MAIN_CLAUDE_ROOT:-${CLAUDE_ROOT:-/claude-roots/main}}"
relax_volume_permissions "${ATTRIBUTION_CLAUDE_ROOT:-/claude-roots/attribution}"
relax_volume_permissions "${PROPOSAL_CLAUDE_ROOT:-/claude-roots/proposal}"

exec "$@"
