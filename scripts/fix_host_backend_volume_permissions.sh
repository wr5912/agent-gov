#!/usr/bin/env bash
set -euo pipefail

TARGET_ROOT=""
APPROVED_ROOT=""
CONFIRM_ROOT=""
CHANGE_ID=""
IMAGE=""
HOST_UID_VALUE=""
HOST_GID_VALUE=""
MODE="dry-run"
MODE_EXPLICIT=""
LAYOUT_MARKER_RELATIVE="data/.agent-gov/runtime-coordination/receipt.json"
MANAGED_RELATIVE_PATHS=(
  "data"
  "governor-workspace"
  "claude-roots/governor"
)

usage() {
  cat <<'USAGE'
Usage: scripts/fix_host_backend_volume_permissions.sh \
  --root ABSOLUTE_RUNTIME_ROOT --approved-root ABSOLUTE_RUNTIME_ROOT \
  --uid HOST_UID --gid HOST_GID [--dry-run]

To apply the reviewed plan, add all of:
  --apply --confirm-root ABSOLUTE_RUNTIME_ROOT \
  --change-id CHANGE_ID --image LOCAL_AGENT_GOV_API_IMAGE

The runtime root must exist, already be canonical, contain the AgentGov runtime
coordination receipt, and contain the three exact backend mounts. Dry-run is the
default. Apply mode mounts only those three directories and never the root.
USAGE
}

die() {
  printf '[volume-permissions] ERROR: %s\n' "$*" >&2
  exit 1
}

die_usage() {
  printf '[volume-permissions] ERROR: %s\n' "$*" >&2
  usage >&2
  exit 2
}

set_mode() {
  local requested_mode=$1
  [[ -z "$MODE_EXPLICIT" || "$MODE_EXPLICIT" == "$requested_mode" ]] \
    || die_usage "--dry-run and --apply are mutually exclusive"
  MODE=$requested_mode
  MODE_EXPLICIT=$requested_mode
}

while (( $# > 0 )); do
  case "$1" in
    --root)
      (( $# >= 2 )) || die_usage "--root requires a value"
      TARGET_ROOT=$2
      shift 2
      ;;
    --approved-root)
      (( $# >= 2 )) || die_usage "--approved-root requires a value"
      APPROVED_ROOT=$2
      shift 2
      ;;
    --confirm-root)
      (( $# >= 2 )) || die_usage "--confirm-root requires a value"
      CONFIRM_ROOT=$2
      shift 2
      ;;
    --change-id)
      (( $# >= 2 )) || die_usage "--change-id requires a value"
      CHANGE_ID=$2
      shift 2
      ;;
    --image)
      (( $# >= 2 )) || die_usage "--image requires a value"
      IMAGE=$2
      shift 2
      ;;
    --uid)
      (( $# >= 2 )) || die_usage "--uid requires a value"
      HOST_UID_VALUE=$2
      shift 2
      ;;
    --gid)
      (( $# >= 2 )) || die_usage "--gid requires a value"
      HOST_GID_VALUE=$2
      shift 2
      ;;
    --dry-run)
      set_mode "dry-run"
      shift
      ;;
    --apply)
      set_mode "apply"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die_usage "unknown or positional argument: $1"
      ;;
  esac
done

for required_name in TARGET_ROOT APPROVED_ROOT HOST_UID_VALUE HOST_GID_VALUE; do
  [[ -n "${!required_name}" ]] || die_usage "missing required field: $required_name"
done

[[ "$TARGET_ROOT" == /* && "$TARGET_ROOT" != "/" ]] \
  || die_usage "--root must be a non-root absolute path"
[[ "$APPROVED_ROOT" == /* && "$APPROVED_ROOT" != "/" ]] \
  || die_usage "--approved-root must be a non-root absolute path"
[[ "$TARGET_ROOT" != *,* && "$APPROVED_ROOT" != *,* ]] \
  || die_usage "runtime root paths must not contain commas"
[[ "$TARGET_ROOT" != *$'\n'* && "$APPROVED_ROOT" != *$'\n'* ]] \
  || die_usage "runtime root paths must not contain newlines"
[[ "$HOST_UID_VALUE" =~ ^[0-9]+$ && "$HOST_GID_VALUE" =~ ^[0-9]+$ ]] \
  || die_usage "--uid and --gid must be numeric"
(( ${#HOST_UID_VALUE} <= 10 && ${#HOST_GID_VALUE} <= 10 )) \
  || die_usage "--uid or --gid is outside the supported range"
(( 10#$HOST_UID_VALUE <= 2147483647 && 10#$HOST_GID_VALUE <= 2147483647 )) \
  || die_usage "--uid or --gid is outside the supported range"

[[ -d "$TARGET_ROOT" && ! -L "$TARGET_ROOT" ]] \
  || die "runtime root must be an existing non-symlink directory"
[[ -d "$APPROVED_ROOT" && ! -L "$APPROVED_ROOT" ]] \
  || die "approved root must be an existing non-symlink directory"

CANONICAL_TARGET_ROOT=$(realpath -e -- "$TARGET_ROOT")
CANONICAL_APPROVED_ROOT=$(realpath -e -- "$APPROVED_ROOT")
[[ "$CANONICAL_TARGET_ROOT" == "$TARGET_ROOT" ]] \
  || die "runtime root path must already be canonical"
[[ "$CANONICAL_APPROVED_ROOT" == "$APPROVED_ROOT" ]] \
  || die "approved root path must already be canonical"
[[ "$CANONICAL_TARGET_ROOT" == "$CANONICAL_APPROVED_ROOT" ]] \
  || die "runtime root does not match the approved canonical root"

LAYOUT_MARKER="$CANONICAL_TARGET_ROOT/$LAYOUT_MARKER_RELATIVE"
[[ -f "$LAYOUT_MARKER" && ! -L "$LAYOUT_MARKER" ]] \
  || die "AgentGov runtime coordination receipt is missing or invalid"
CANONICAL_LAYOUT_MARKER=$(realpath -e -- "$LAYOUT_MARKER")
[[ "$CANONICAL_LAYOUT_MARKER" == "$LAYOUT_MARKER" ]] \
  || die "AgentGov runtime coordination receipt must not traverse symlinks"

MANAGED_HOST_PATHS=()
for relative_path in "${MANAGED_RELATIVE_PATHS[@]}"; do
  host_path="$CANONICAL_TARGET_ROOT/$relative_path"
  [[ -d "$host_path" && ! -L "$host_path" ]] \
    || die "managed backend directory is missing or is a symlink: $relative_path"
  canonical_host_path=$(realpath -e -- "$host_path")
  [[ "$canonical_host_path" == "$host_path" && "$canonical_host_path" == "$CANONICAL_TARGET_ROOT/"* ]] \
    || die "managed backend directory escapes the approved runtime root: $relative_path"
  MANAGED_HOST_PATHS+=("$host_path")
done

printf '[volume-permissions] mode=%s root=%s uid=%s gid=%s\n' \
  "$MODE" "$CANONICAL_TARGET_ROOT" "$HOST_UID_VALUE" "$HOST_GID_VALUE"
printf '[volume-permissions] managed=%s\n' "${MANAGED_RELATIVE_PATHS[*]}"

if [[ "$MODE" == "dry-run" ]]; then
  printf '[volume-permissions] dry-run complete; no filesystem changes were made\n'
  exit 0
fi

[[ "$CONFIRM_ROOT" == "$CANONICAL_TARGET_ROOT" ]] \
  || die_usage "--confirm-root must exactly match the approved canonical root"
[[ "$CHANGE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$ ]] \
  || die_usage "--change-id is required and invalid"
[[ -n "$IMAGE" && "$IMAGE" != -* && "$IMAGE" != *[[:space:]]* ]] \
  || die_usage "--image must name one explicit local image without whitespace"
command -v docker >/dev/null 2>&1 || die "docker is required in apply mode"
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || die "the explicitly selected local image is unavailable"

docker run --rm \
  --network none \
  --read-only \
  --user 0:0 \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add FOWNER \
  --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  --entrypoint sh \
  -e "HOST_UID=$HOST_UID_VALUE" \
  -e "HOST_GID=$HOST_GID_VALUE" \
  --mount "type=bind,src=${MANAGED_HOST_PATHS[0]},dst=/approved/data" \
  --mount "type=bind,src=${MANAGED_HOST_PATHS[1]},dst=/approved/governor-workspace" \
  --mount "type=bind,src=${MANAGED_HOST_PATHS[2]},dst=/approved/governor-claude-root" \
  "$IMAGE" -eu -c '
    for path in \
      /approved/data \
      /approved/governor-workspace \
      /approved/governor-claude-root
    do
      [ -d "$path" ] && [ ! -L "$path" ] || exit 1
      find "$path" -xdev -type d -exec chown "$HOST_UID:$HOST_GID" {} +
      find "$path" -xdev -type f -exec chown "$HOST_UID:$HOST_GID" {} +
      find "$path" -xdev -type d -exec chmod ug+rwx {} +
      find "$path" -xdev -type f -exec chmod ug+rw {} +
    done
  '

printf '[volume-permissions] applied change=%s root=%s\n' "$CHANGE_ID" "$CANONICAL_TARGET_ROOT"
