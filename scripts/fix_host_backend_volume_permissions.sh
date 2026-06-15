#!/usr/bin/env bash
set -euo pipefail

HOST_UID_VALUE="${HOST_UID:-$(id -u)}"
HOST_GID_VALUE="${HOST_GID:-$(id -g)}"
IMAGE="${AGENT_GOV_FIX_IMAGE:-}"
TARGET_ROOT="${HOST_RUNTIME_VOLUME_ROOT:-${RUNTIME_VOLUME_ROOT:-${HOME}/volume-agent-gov}}"

if [[ -z "$IMAGE" ]]; then
  IMAGE="$(docker images --format '{{.Repository}}:{{.Tag}}' | awk -F: '$1=="agent-gov-api" {print; exit}')"
fi

if [[ -z "$IMAGE" ]]; then
  echo "No agent-gov-api image found. Build the backend image first." >&2
  exit 1
fi

docker run --rm --user 0:0 \
  -e HOST_UID="$HOST_UID_VALUE" \
  -e HOST_GID="$HOST_GID_VALUE" \
  -v "$TARGET_ROOT:/target" \
  "$IMAGE" sh -eu -c '
    for path in \
      main-workspace governor-workspace \
      data \
      claude-roots/main claude-roots/governor
    do
      [ -e "/target/$path" ] || continue
      chown -R "$HOST_UID:$HOST_GID" "/target/$path"
      find "/target/$path" -type d -exec chmod ug+rwx {} +
      find "/target/$path" -type f -exec chmod ug+rw {} +
    done
  '
