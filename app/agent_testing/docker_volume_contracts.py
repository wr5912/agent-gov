"""Docker volume 与 agent-test sandbox 的纯 stdlib 身份契约。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

SANDBOX_KIND_LABEL: Final = "io.agentgov.agent-test.sandbox"
SANDBOX_SCOPE_LABEL: Final = "io.agentgov.agent-test.runtime-scope"

_VOLUME_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")


def validated_local_volume_mountpoint(volume: Mapping[str, object], *, expected_name: str) -> Path:
    """校验 exact local named-volume inspect，并返回 daemon 权威根 Mountpoint。"""
    expected = validate_docker_volume_name(expected_name)
    inspected_name = validate_docker_volume_name(volume.get("Name"))
    if inspected_name != expected:
        raise ValueError("volume inspect identity mismatch")
    if volume.get("Driver") != "local" or volume.get("Scope") != "local":
        raise ValueError("runs volume must use the local Docker driver")
    if volume.get("Options") not in (None, {}):
        raise ValueError("runs volume driver options must be empty")
    return normalized_mount_path(volume.get("Mountpoint"), field="volume mountpoint")


def validate_docker_volume_name(value: object) -> str:
    if not isinstance(value, str) or _VOLUME_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError("Docker volume name is invalid")
    return value


def normalized_mount_path(value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} is invalid")
    return path
