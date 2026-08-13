"""容器验收 verifier 受管环境与一次性 re-exec transport 契约。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from scripts import container_acceptance_make_gate as acceptance_make_gate
from scripts import container_acceptance_toolchain as acceptance_toolchain

MANAGED_ENVIRONMENT_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_MANAGED_ENVIRONMENT_SHA256"
INTERNAL_REEXEC_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_PYTHON_SHA256",
        "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_IMPORT_AUTHORITY_SHA256",
        "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_STAGE",
        "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_TOOLCHAIN_SHA256",
        "AGENT_GOV_ACCEPTANCE_LOCK_COOKIE",
        "AGENT_GOV_ACCEPTANCE_LOCK_FD",
        "AGENT_GOV_ACCEPTANCE_LOADED_BOOTSTRAP_SHA256",
        "AGENT_GOV_ACCEPTANCE_REEXEC_STAGE",
        "AGENT_GOV_ACCEPTANCE_SNAPSHOT_ROOT",
        "AGENT_GOV_ACCEPTANCE_SIGNAL_HANDOFF",
        "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_EVIDENCE",
        "AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY",
        "AGENT_GOV_PREPARED_RECEIPT_AUTHORITY",
        "AGENT_GOV_RESERVED_RECEIPT_AUTHORITY",
    }
)
PREPARED_REEXEC_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "AGENT_GOV_ACCEPTANCE_LOCK_COOKIE",
        "AGENT_GOV_ACCEPTANCE_LOCK_FD",
        "AGENT_GOV_ACCEPTANCE_REEXEC_STAGE",
        "AGENT_GOV_ACCEPTANCE_SNAPSHOT_ROOT",
        "AGENT_GOV_ACCEPTANCE_SIGNAL_HANDOFF",
        "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_EVIDENCE",
        "AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY",
        "AGENT_GOV_PREPARED_RECEIPT_AUTHORITY",
    }
)
_PROXY_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
PRESERVED_ENVIRONMENT_KEYS: Final = frozenset(
    {
        *_PROXY_ENVIRONMENT_KEYS,
        "LANG",
        "LANGUAGE",
        "TZ",
    }
)
_MANAGED_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE",
        "AGENT_GOV_ACCEPTANCE_COMPOSE_PLUGIN",
        "AGENT_GOV_ACCEPTANCE_DOCKER",
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_BYTES",
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_ENTRIES",
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_SHA256",
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCY_ROOT",
        "AGENT_GOV_ACCEPTANCE_GIT",
        "AGENT_GOV_ACCEPTANCE_MAKE",
        "AGENT_GOV_ACCEPTANCE_NODE",
        "AGENT_GOV_ACCEPTANCE_PNPM",
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_BYTES",
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_ENTRIES",
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_SHA256",
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCY_ROOT",
        "AGENT_GOV_ACCEPTANCE_PYTHON",
        "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY",
        "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY_SHA256",
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_BYTES",
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_ENTRIES",
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_SHA256",
        "AGENT_GOV_ACCEPTANCE_PYTHON_SITE_PACKAGES",
        "AGENT_GOV_ACCEPTANCE_PYTHON_TOOLCHAIN_SHA256",
        "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT",
        "AGENT_GOV_ACCEPTANCE_RUN_ID",
        "AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256",
        "AGENT_GOV_ACCEPTANCE_SYSTEM_PYTHON_SHA256",
        "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_SHA256",
        "AGENT_GOV_COMPOSE_ENV_FILE",
        "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE",
        "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE",
        "AGENT_TEST_RUN_TIMEOUT_SECONDS",
        "AGENT_TEST_WORKER_POLL_SECONDS",
        "API_BASE",
        "API_KEY",
        "APP_VERSION",
        "BUILDX_CONFIG",
        "COMPOSE_ENV_FILE",
        "COMPOSE_PROJECT_NAME",
        "CONTAINER_NAME_PREFIX",
        "DOCKER_CONFIG",
        "DOCKER_CLI_PLUGIN_EXTRA_DIRS",
        "DOCKER_HOST",
        "FRONTEND_HOST_PORT",
        "FRONTEND_RUNTIME_API_BASE",
        "FRONTEND_RUNTIME_API_KEY",
        "HOME",
        "HOST_DATA_MOUNT",
        "HOST_GOVERNOR_CLAUDE_ROOT_MOUNT",
        "HOST_GOVERNOR_WORKSPACE_MOUNT",
        "HOST_PORT",
        "HOST_RUNTIME_VOLUME_ROOT",
        "LANGFUSE_CLICKHOUSE_DATA_MOUNT",
        "LANGFUSE_CLICKHOUSE_LOGS_MOUNT",
        "LANGFUSE_MINIO_DATA_MOUNT",
        "LANGFUSE_POSTGRES_DATA_MOUNT",
        "LANGFUSE_REDIS_DATA_MOUNT",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "PATH",
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
        "TMPDIR",
        "VERIFY_SCREENSHOT_DIR",
        "XDG_CONFIG_HOME",
    }
)
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ENVIRONMENT_ENTRIES: Final = 96
_MAX_ENVIRONMENT_VALUE_BYTES: Final = 32 * 1024


class AcceptanceContractError(RuntimeError):
    """验收 verifier、受管环境或持久化回执未满足固定契约。"""


class ManagedAcceptanceEnvironment(dict[str, str]):
    """仅承载最终传入 Make/verifier 的稳定受管键。"""


class ReexecTransportEnvironment(dict[str, str]):
    """只跨一次 snapshot re-exec 的内部 authority。"""


class PreparedReexecEnvironment(dict[str, str]):
    """受管 verifier 环境与一次性内部 authority 的合并 re-exec 环境。"""


class CandidateRuntimeEnvironment(dict[str, str]):
    """候选快照内私有 runtime 目录的受管环境。"""


class TerminalProcessEnvironment(dict[str, str]):
    """candidate 删除后仅供固定 Docker 查询使用的最小环境。"""


@dataclass(frozen=True, slots=True)
class CandidateRuntimeEnvironmentPaths:
    root: Path
    home: Path
    xdg_config: Path
    buildx_config: Path
    temporary: Path
    screenshots: Path

    def managed_values(self) -> CandidateRuntimeEnvironment:
        return CandidateRuntimeEnvironment(
            {
                "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT": str(self.root),
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.xdg_config),
                "BUILDX_CONFIG": str(self.buildx_config),
                "TMPDIR": str(self.temporary),
                "VERIFY_SCREENSHOT_DIR": str(self.screenshots),
            }
        )


def candidate_runtime_environment_paths(runtime_root: Path) -> CandidateRuntimeEnvironmentPaths:
    absolute = Path(runtime_root)
    if not absolute.is_absolute() or absolute != Path(str(absolute)) or any(part in {"", ".", ".."} for part in absolute.parts[1:]):
        raise AcceptanceContractError("candidate runtime environment root is invalid")
    return CandidateRuntimeEnvironmentPaths(
        absolute,
        absolute / "home",
        absolute / "xdg-config",
        absolute / "buildx",
        absolute / "tmp",
        absolute / "screenshots",
    )


def _require_candidate_runtime_values(values: Mapping[str, str]) -> None:
    raw_root = values.get("AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT")
    if not isinstance(raw_root, str):
        raise AcceptanceContractError("candidate runtime environment is missing")
    expected = candidate_runtime_environment_paths(Path(raw_root)).managed_values()
    if any(values.get(key) != value for key, value in expected.items()):
        raise AcceptanceContractError("candidate runtime environment is invalid")


def _require_snapshot_dependency_values(values: Mapping[str, str]) -> None:
    frontend = values.get(acceptance_toolchain.FRONTEND_DEPENDENCY_ROOT_ENV)
    python = values.get(acceptance_toolchain.PYTHON_SITE_PACKAGES_ENV)
    pnpm = values.get(acceptance_toolchain.PNPM_DEPENDENCY_ROOT_ENV)
    if (
        not isinstance(frontend, str)
        or not isinstance(python, str)
        or not isinstance(pnpm, str)
        or not Path(frontend).is_absolute()
        or not Path(python).is_absolute()
        or not Path(pnpm).is_absolute()
        or Path(frontend).name != "node_modules"
        or Path(python).name != "python-site-packages"
        or Path(pnpm).name != "pnpm"
    ):
        raise AcceptanceContractError("prepared dependency roots are invalid")
    for prefix in ("FRONTEND", "PYTHON", "PNPM"):
        digest = values.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_SHA256")
        entries = values.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_ENTRIES")
        regular_bytes = values.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_BYTES")
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(entries, str)
            or not entries.isdecimal()
            or int(entries) < 1
            or not isinstance(regular_bytes, str)
            or not regular_bytes.isdecimal()
        ):
            raise AcceptanceContractError("prepared dependency evidence is invalid")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _valid_environment_entry(key: str, value: str) -> bool:
    return (
        _ENVIRONMENT_KEY.fullmatch(key) is not None
        and "\x00" not in value
        and len(value.encode("utf-8", errors="surrogateescape")) <= _MAX_ENVIRONMENT_VALUE_BYTES
    )


def controlled_acceptance_path(
    environ: Mapping[str, str],
    *,
    frontend_dependency_root: Path | None = None,
    pnpm_dependency_root: Path | None = None,
) -> str:
    try:
        acceptance_toolchain.initialize_toolchain_authority(environ)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise AcceptanceContractError("fixed acceptance toolchain authority is invalid") from exc
    return acceptance_toolchain.controlled_path(frontend_dependency_root, pnpm_dependency_root)


def _is_allowed_managed_key(key: str) -> bool:
    return key in PRESERVED_ENVIRONMENT_KEYS or key in _MANAGED_ENVIRONMENT_KEYS or key.startswith("LC_")


def _safe_preserved_environment_value(key: str, value: str) -> bool:
    if not _valid_environment_entry(key, value) or not value or "\n" in value or "\r" in value:
        return False
    if key in _PROXY_ENVIRONMENT_KEYS and key.lower() != "no_proxy":
        return value.startswith(("http://", "https://", "socks5://", "socks5h://"))
    return True


def managed_environment_sha256(environ: Mapping[str, str]) -> str:
    try:
        normalized = acceptance_make_gate.managed_environment_from_gate_spawn(environ)
    except acceptance_make_gate.MakeGateError as exc:
        raise AcceptanceContractError("managed Make gate environment is invalid") from exc
    normalized.pop(MANAGED_ENVIRONMENT_SHA256_ENV, None)
    if (
        len(normalized) > _MAX_ENVIRONMENT_ENTRIES
        or normalized.get("LITELLM_LOCAL_MODEL_COST_MAP") != "True"
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in normalized.items())
        or any(not _is_allowed_managed_key(key) or not _valid_environment_entry(key, value) for key, value in normalized.items())
    ):
        raise AcceptanceContractError("managed acceptance environment is invalid")
    _require_candidate_runtime_values(normalized)
    _require_snapshot_dependency_values(normalized)
    return hashlib.sha256(_canonical_json(normalized)).hexdigest()


def build_managed_environment(
    environ: Mapping[str, str],
    *,
    managed_values: Mapping[str, str],
) -> ManagedAcceptanceEnvironment:
    frontend_root = managed_values.get(acceptance_toolchain.FRONTEND_DEPENDENCY_ROOT_ENV)
    pnpm_root = managed_values.get(acceptance_toolchain.PNPM_DEPENDENCY_ROOT_ENV)
    if not isinstance(frontend_root, str) or not isinstance(pnpm_root, str):
        raise AcceptanceContractError("prepared dependency root is missing")
    controlled_path = controlled_acceptance_path(
        environ,
        frontend_dependency_root=Path(frontend_root),
        pnpm_dependency_root=Path(pnpm_root),
    )
    _require_candidate_runtime_values(managed_values)
    _require_snapshot_dependency_values(managed_values)
    preserved = {
        key: value
        for key, value in environ.items()
        if (key in PRESERVED_ENVIRONMENT_KEYS or key.startswith("LC_")) and _safe_preserved_environment_value(key, value)
    }
    child = ManagedAcceptanceEnvironment(preserved)
    child.update(managed_values)
    child.update(
        acceptance_toolchain.managed_tool_environment(
            frontend_dependency_root=Path(frontend_root),
            pnpm_dependency_root=Path(pnpm_root),
        )
    )
    child["PATH"] = controlled_path
    child[acceptance_toolchain.TOOLCHAIN_SHA256_ENV] = acceptance_toolchain.toolchain_sha256()
    digest = managed_environment_sha256(child)
    child[MANAGED_ENVIRONMENT_SHA256_ENV] = digest
    return child


def _validate_managed_environment_identity(environ: Mapping[str, str]) -> str:
    digest = environ.get(MANAGED_ENVIRONMENT_SHA256_ENV)
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None or digest != managed_environment_sha256(environ):
        raise AcceptanceContractError("managed acceptance environment digest is invalid")
    if environ.get(acceptance_toolchain.TOOLCHAIN_SHA256_ENV) != acceptance_toolchain.toolchain_sha256():
        raise AcceptanceContractError("managed acceptance toolchain digest is invalid")
    return digest


def validate_managed_environment(environ: Mapping[str, str]) -> str:
    digest = _validate_managed_environment_identity(environ)
    try:
        acceptance_toolchain.validate_execution_tool_authority(environ)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise AcceptanceContractError("fixed acceptance execution authority drifted") from exc
    return digest


def validate_terminal_managed_environment(environ: Mapping[str, str]) -> str:
    digest = _validate_managed_environment_identity(environ)
    try:
        acceptance_toolchain.validate_execution_tool_authority()
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise AcceptanceContractError("fixed terminal execution authority drifted") from exc
    return digest


def terminal_process_environment(environ: Mapping[str, str]) -> TerminalProcessEnvironment:
    validate_terminal_managed_environment(environ)
    child = TerminalProcessEnvironment(
        {key: value for key, value in environ.items() if key in {"DOCKER_HOST", "DOCKER_CONFIG", "LANG", "LANGUAGE", "TZ"} or key.startswith("LC_")}
    )
    child["PATH"] = "/usr/bin"
    if set(("DOCKER_HOST", "DOCKER_CONFIG")) - set(child):
        raise AcceptanceContractError("terminal Docker environment is incomplete")
    return child


def build_prepared_reexec_environment(
    managed: Mapping[str, str],
    *,
    transport_values: Mapping[str, str],
) -> PreparedReexecEnvironment:
    validate_managed_environment(managed)
    required_without_evidence = PREPARED_REEXEC_ENVIRONMENT_KEYS - {acceptance_toolchain.TOOLCHAIN_EVIDENCE_ENV}
    if set(transport_values) != required_without_evidence or any(not _valid_environment_entry(key, value) for key, value in transport_values.items()):
        raise AcceptanceContractError("prepared acceptance re-exec transport is invalid")
    evidence = acceptance_toolchain.serialized_authority_environment(acceptance_toolchain.initialize_toolchain_authority(managed))
    if evidence.get(acceptance_toolchain.TOOLCHAIN_SHA256_ENV) != managed.get(acceptance_toolchain.TOOLCHAIN_SHA256_ENV):
        raise AcceptanceContractError("prepared acceptance re-exec toolchain digest is invalid")
    combined = PreparedReexecEnvironment(managed)
    combined.update(transport_values)
    combined[acceptance_toolchain.TOOLCHAIN_EVIDENCE_ENV] = evidence[acceptance_toolchain.TOOLCHAIN_EVIDENCE_ENV]
    return combined


def split_reexec_environment(
    environ: Mapping[str, str],
) -> tuple[ReexecTransportEnvironment, ManagedAcceptanceEnvironment]:
    transport = ReexecTransportEnvironment({key: value for key, value in environ.items() if key in INTERNAL_REEXEC_ENVIRONMENT_KEYS})
    if set(transport) != PREPARED_REEXEC_ENVIRONMENT_KEYS or any(not _valid_environment_entry(key, value) for key, value in transport.items()):
        raise AcceptanceContractError("prepared acceptance re-exec transport is invalid")
    try:
        acceptance_toolchain.initialize_toolchain_authority(environ)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise AcceptanceContractError("prepared acceptance re-exec toolchain authority is invalid") from exc
    managed = ManagedAcceptanceEnvironment({key: value for key, value in environ.items() if key not in INTERNAL_REEXEC_ENVIRONMENT_KEYS})
    validate_managed_environment(managed)
    return transport, managed
