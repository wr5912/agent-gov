"""Sanitized selected-env derivation; destructive image cutover is retired."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    from scripts.agentscope_atomic_cutover_env import declared_env_keys, trusted_operator_identity
except ModuleNotFoundError:
    from agentscope_atomic_cutover_env import declared_env_keys, trusted_operator_identity

_CUTOVER_CONTROL_ENV_KEYS = {
    "COMPOSE",
    "COMPOSE_ANSI",
    "COMPOSE_DISABLE_ENV_FILE",
    "COMPOSE_ENV_FILES",
    "COMPOSE_EXPERIMENTAL",
    "COMPOSE_FILE",
    "COMPOSE_IGNORE_ORPHANS",
    "COMPOSE_MENU",
    "COMPOSE_PARALLEL_LIMIT",
    "COMPOSE_PATH_SEPARATOR",
    "COMPOSE_PROFILES",
    "COMPOSE_PROGRESS",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_STATUS_STDOUT",
    "GNUMAKEFLAGS",
    "LANGFUSE_COMPOSE",
    "MAKEFILES",
    "MAKEFLAGS",
    "MAKELEVEL",
    "MAKEOVERRIDES",
    "MFLAGS",
}
_HOST_CAPABILITY_ENV_KEYS = {
    "DOCKER_API_VERSION",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "XDG_RUNTIME_DIR",
}
_TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_SAFE_EXPLICIT_KEYS = {
    "AGENTGOV_SOURCE_ARTIFACT_SHA256",
    "AGENT_GOV_ACCEPTANCE_RUN_ID",
    "APP_VERSION",
    "COMPOSE_PROJECT_NAME",
    "RUNTIME_BOOTSTRAP_HOST_DIR",
}
_COMPOSE_INTERPOLATION = re.compile(r"(?<!\$)\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")


def compose_interpolation_keys(compose_files: Sequence[Path]) -> set[str]:
    keys: set[str] = set()
    for compose_file in compose_files:
        for match in _COMPOSE_INTERPOLATION.finditer(compose_file.read_text(encoding="utf-8")):
            keys.add(match.group("braced") or match.group("plain"))
    return keys


def selected_env_child_env(
    env_file: Path,
    *,
    explicit: Mapping[str, str] | None = None,
    compose_files: Sequence[Path] = (),
) -> dict[str, str]:
    """Build a capability-only child env with selected configuration locked out of ambient state."""

    selected_keys = declared_env_keys(env_file)
    blocked = selected_keys | compose_interpolation_keys(compose_files) | _CUTOVER_CONTROL_ENV_KEYS
    child_env = {key: value for key, value in os.environ.items() if key in _HOST_CAPABILITY_ENV_KEYS and key not in blocked}
    child_env["PATH"] = _TRUSTED_PATH
    if "HOME" not in selected_keys:
        child_env["HOME"] = trusted_operator_identity().home.as_posix()
    if explicit:
        unexpected = set(explicit) - _SAFE_EXPLICIT_KEYS
        if unexpected:
            raise ValueError(f"selected-env explicit child env 含未授权键: {sorted(unexpected)}")
        child_env.update(explicit)
    return child_env


def selected_compose_child_env(repo_root: Path, env_file: Path, compose_files: Sequence[Path]) -> dict[str, str]:
    """Lock the version truth needed to render Compose without ambient interpolation."""

    version = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION 不得为空")
    return selected_env_child_env(env_file, explicit={"APP_VERSION": version}, compose_files=compose_files)


class CutoverImageSupport:
    """Tombstone for the retired destructive image workflow."""

    def __init__(self, *, error_type: type[RuntimeError], **_unused: object) -> None:
        raise error_type("atomic cutover image mutators 已退役；只允许 selected-env helper")
