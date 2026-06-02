#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_UID_VALUE="${HOST_UID:-$(id -u)}"
HOST_GID_VALUE="${HOST_GID:-$(id -g)}"
IMAGE="${CLAUDE_AGENT_RUNTIME_FIX_IMAGE:-}"

if [[ -z "$IMAGE" ]]; then
  IMAGE="$(docker images --format '{{.Repository}}:{{.Tag}}' | awk -F: '$1=="claude-agent-runtime-api" {print; exit}')"
fi

if [[ -z "$IMAGE" ]]; then
  echo "No claude-agent-runtime-api image found. Build the backend image first." >&2
  exit 1
fi

docker run --rm --user 0:0 \
  -e HOST_UID="$HOST_UID_VALUE" \
  -e HOST_GID="$HOST_GID_VALUE" \
  -v "$ROOT_DIR/docker/volume:/target" \
  "$IMAGE" sh -eu -c '
    for path in \
      main-workspace attribution-analyzer-workspace proposal-generator-workspace \
      execution-optimizer-workspace eval-case-governor-workspace regression-impact-analyzer-workspace \
      data \
      claude-roots/main claude-roots/attribution-analyzer claude-roots/proposal-generator \
      claude-roots/execution-optimizer claude-roots/eval-case-governor claude-roots/regression-impact-analyzer
    do
      [ -e "/target/$path" ] || continue
      chown -R "$HOST_UID:$HOST_GID" "/target/$path"
      find "/target/$path" -type d -exec chmod ug+rwx {} +
      find "/target/$path" -type f -exec chmod ug+rw {} +
    done
  '
