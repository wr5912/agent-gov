"""selected-env 部署操作的固定注册表与类型契约。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias

from scripts.agentscope_atomic_cutover_env import parse_selected_env_bindings
from scripts.agentscope_atomic_cutover_types import DockerDaemonIdentity

OPERATIONS = (
    "all-up",
    "build",
    "check",
    "compose-diagnose",
    "down",
    "images-prepare",
    "langfuse-logs",
    "langfuse-prepare",
    "langfuse-stop",
    "langfuse-up",
    "logs",
    "runtime-bootstrap",
    "runtime-clean",
    "runtime-migrate",
    "runtime-migrate-scan",
    "runtime-prepare-harnesses",
    "runtime-recreate",
    "runtime-validate",
    "ui-build",
    "ui-logs",
    "ui-playground-deployed-smoke",
    "ui-recreate",
    "ui-stop",
    "ui-up",
    "up",
)
START_OPERATIONS = frozenset(
    {
        "all-up",
        "ui-playground-deployed-smoke",
        "langfuse-up",
        "runtime-bootstrap",
        "runtime-prepare-harnesses",
        "runtime-recreate",
        "ui-recreate",
        "ui-up",
        "up",
    },
)
DOCKER_BIND_OPERATIONS = frozenset(
    {
        "all-up",
        "ui-playground-deployed-smoke",
        "langfuse-up",
        "runtime-prepare-harnesses",
        "runtime-recreate",
        "ui-recreate",
        "up",
    },
)
DOCKER_MUTATING_OPERATIONS = frozenset(
    {
        *DOCKER_BIND_OPERATIONS,
        "build",
        "down",
        "images-prepare",
        "langfuse-prepare",
        "langfuse-stop",
        "ui-build",
        "ui-up",
        "ui-stop",
    },
)
HOST_MUTATING_OPERATIONS = frozenset(
    {
        "runtime-bootstrap",
        "runtime-clean",
        "runtime-migrate",
    }
)
MUTATING_OPERATIONS = DOCKER_MUTATING_OPERATIONS | HOST_MUTATING_OPERATIONS
BUILD_OPERATIONS = frozenset({"build", "ui-build", "ui-playground-deployed-smoke"})
SOURCE_FREEZE_OPERATIONS = frozenset(OPERATIONS)
PREFLIGHT_OPERATIONS = frozenset(
    {
        "check",
        "langfuse-prepare",
        "langfuse-up",
        "runtime-bootstrap",
        "runtime-clean",
        "runtime-migrate",
        "runtime-migrate-scan",
        "runtime-prepare-harnesses",
        "runtime-recreate",
        "ui-recreate",
        "runtime-validate",
        "ui-up",
    },
)
IMAGE_SCOPES = {
    "runtime-recreate": ("agentscope-runtime",),
    "ui-build": ("agent-gov-ui",),
    "ui-recreate": ("agent-gov-ui",),
    "ui-up": ("agent-gov-ui",),
}
RETIRED_CUTOVER_KEYS = (
    "AGENTGOV_ACCEPTANCE_API_KEY",
    "AGENTGOV_ACCEPTANCE_IDENTITY",
    "AGENTGOV_API_GATE_STATE_DIR_HOST",
    "AGENTGOV_API_GATE_STATE_FILE",
    "AGENTGOV_CUTOVER_ACCEPTANCE_ONLY",
    "AGENT_GOV_ACCEPTANCE_RUN_ID",
)
LOCAL_IMAGES = {
    "agent-gov-api": "agent-gov-api",
    "agent-gov-ui": "agent-gov-ui",
    "agentscope-runtime": "agent-gov-agentscope-runtime",
}
THIRD_PARTY_SERVICES = (
    "langfuse-clickhouse",
    "langfuse-minio",
    "langfuse-postgres",
    "langfuse-redis",
    "langfuse-web",
    "langfuse-worker",
)
DIGEST_REFERENCE = re.compile(r".+@sha256:[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")

OperationCommands: TypeAlias = dict[str, list[str]]
OperationEnvironment: TypeAlias = dict[str, str]
StackImageIds: TypeAlias = dict[str, str]
ImageReferences: TypeAlias = dict[str, str]
ComposeServices: TypeAlias = Mapping[str, object]
DaemonBoundary: TypeAlias = tuple[DockerDaemonIdentity | None, str | None, StackImageIds | None]


class SelectedEnvError(RuntimeError):
    """The selected deployment env cannot be pinned safely."""


class MissingImageError(SelectedEnvError):
    """A digest-pinned image is confirmed absent from the bound local daemon."""


def require_current_epoch_env(snapshot: Path, operation: str) -> None:
    if operation not in START_OPERATIONS:
        return
    values = {binding.key: binding.value or "" for binding in parse_selected_env_bindings(snapshot) if binding.key}
    if values.get("AGENTGOV_API_MODE") != "open":
        raise SelectedEnvError("普通部署要求所选 env 明确设置 AGENTGOV_API_MODE=open")
    api_key = values.get("API_KEY", "").strip()
    if not api_key or values.get("FRONTEND_RUNTIME_API_KEY", "").strip() != api_key:
        raise SelectedEnvError("普通部署要求 API_KEY 与 FRONTEND_RUNTIME_API_KEY 非空且精确一致")
    populated = sorted(key for key in RETIRED_CUTOVER_KEYS if values.get(key, "").strip())
    if populated:
        raise SelectedEnvError(f"普通部署不得携带已退役 cutover 状态: {populated}")


def simple_operation_commands(base: list[str], langfuse: list[str]) -> OperationCommands:
    """Return the fixed non-orchestrated Compose command registry."""
    return {
        "down": [*base, "down"],
        "logs": [*base, "logs", "-f", "agent-gov-api", "agentscope-runtime"],
        "ui-build": [*base, "build", "--pull=false", "agent-gov-ui"],
        "ui-recreate": [
            *base,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--pull",
            "never",
            "--no-build",
            "agent-gov-ui",
        ],
        "ui-up": [*base, "up", "-d", "--pull", "never", "agent-gov-ui"],
        "runtime-recreate": [
            *base,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--pull",
            "never",
            "--no-build",
            "agentscope-runtime",
        ],
        "ui-stop": [*base, "stop", "agent-gov-ui"],
        "ui-logs": [*base, "logs", "-f", "agent-gov-ui"],
        "langfuse-stop": [
            *langfuse,
            "--profile",
            "langfuse",
            "stop",
            "langfuse-worker",
            "langfuse-web",
            "langfuse-minio",
            "langfuse-redis",
            "langfuse-clickhouse",
            "langfuse-postgres",
        ],
        "langfuse-logs": [
            *langfuse,
            "--profile",
            "langfuse",
            "logs",
            "-f",
            "langfuse-web",
            "langfuse-worker",
        ],
        "compose-diagnose": ["bash", "scripts/compose_diagnose.sh"],
    }
