#!/usr/bin/env sh
set -eu

relax_volume_permissions() {
    path="$1"
    if [ -e "$path" ]; then
        chmod -R a+rwX "$path" 2>/dev/null || true
    fi
}

relax_volume_permissions "${WORKSPACE_DIR:-/workspace}"
relax_volume_permissions "${DATA_DIR:-/data}"
relax_volume_permissions "${CLAUDE_ROOT:-/root}"

exec "$@"
