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

# governor 是唯一顶层特殊 Agent；业务 Agent（含预制 main-agent）的 workspace/claude-root 落在
# /data 下，由下方 DATA_DIR 一并放权——不再单独建 /main-workspace、/claude-roots/main（已随 B 整改去除）。
ensure_claude_config_dir "${GOVERNOR_CLAUDE_ROOT:-/claude-roots/governor}"

relax_volume_permissions "${GOVERNOR_WORKSPACE_DIR:-/governor-workspace}"
relax_volume_permissions "${DATA_DIR:-/data}"
relax_volume_permissions "${GOVERNOR_CLAUDE_ROOT:-/claude-roots/governor}"

# 两个迁移必须位于递归卷放权之后，避免私有 backup/audit 的 0700/0600 权限被再次放宽。
python /app/scripts/reconcile_business_agent_workspace_policy.py \
    --runtime-root / \
    --runtime-volume-mode container \
    --template-dir /app/docker/runtime-volume-seeds \
    --apply \
    --operator container-entrypoint \
    --quiet

python /app/scripts/retire_runtime_seed_assets.py \
    --runtime-root / \
    --runtime-volume-mode container \
    --registry /app/docker/runtime-volume-seeds/workspace-policy/retired-seed-assets.json \
    --apply \
    --operator container-entrypoint \
    --quiet

exec "$@"
