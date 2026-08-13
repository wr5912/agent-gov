"""公共容器验收 launcher、宿主工具与依赖的固定 authority。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, TypedDict, cast

from scripts import container_acceptance_import_authority as import_authority

REPO_ROOT, REPOSITORY_IMPORT_ROOT = import_authority.repository_roots(globals(), __file__)
if str(REPOSITORY_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_IMPORT_ROOT))

from scripts import container_acceptance_dependency_authority as dependency_authority  # noqa: E402
from scripts import container_acceptance_docker_authority as docker_authority  # noqa: E402
from scripts import container_acceptance_git_authority as git_authority  # noqa: E402
from scripts import container_acceptance_python_runner as python_runner  # noqa: E402
from scripts import container_acceptance_snapshot_exec as snapshot_exec  # noqa: E402
from scripts import container_acceptance_tool_authority as tool_authority  # noqa: E402
from scripts.container_acceptance_dependency_requirements import (  # noqa: E402
    CandidateSnapshotParentRequirement,
    FrontendDependencyProjectionRequirement,
    NodeExecutableSnapshotRequirement,
    PnpmDependencySnapshotRequirement,
    PythonDependencySnapshotRequirement,
    ReceiptRootRequirement,
    frontend_requirement,
    node_requirement,
    pnpm_requirement,
    python_requirement,
)

TOOLCHAIN_CONTRACT: Final = "agentgov.container-acceptance-toolchain.v2"
TOOLCHAIN_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_SHA256"
TOOLCHAIN_EVIDENCE_ENV: Final = "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_EVIDENCE"
BOOTSTRAP_PYTHON_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_PYTHON_SHA256"
BOOTSTRAP_TOOLCHAIN_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_TOOLCHAIN_SHA256"
BOOTSTRAP_STAGE_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_STAGE"
SYSTEM_PYTHON_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_SYSTEM_PYTHON_SHA256"
PYTHON_EXECUTABLE_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON"
NODE_EXECUTABLE_ENV: Final = "AGENT_GOV_ACCEPTANCE_NODE"
PNPM_EXECUTABLE_ENV: Final = "AGENT_GOV_ACCEPTANCE_PNPM"
PNPM_DEPENDENCY_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCY_ROOT"
GIT_EXECUTABLE_ENV: Final = "AGENT_GOV_ACCEPTANCE_GIT"
DOCKER_EXECUTABLE_ENV: Final = "AGENT_GOV_ACCEPTANCE_DOCKER"
COMPOSE_PLUGIN_ENV: Final = "AGENT_GOV_ACCEPTANCE_COMPOSE_PLUGIN"
MAKE_EXECUTABLE_ENV: Final = "AGENT_GOV_ACCEPTANCE_MAKE"
SNAPSHOT_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_SNAPSHOT_ROOT"
FRONTEND_DEPENDENCY_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCY_ROOT"
PYTHON_SITE_PACKAGES_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_SITE_PACKAGES"
PYTHON_AUTHORITY_ENV: Final = python_runner.PYTHON_AUTHORITY_ENV
PYTHON_AUTHORITY_SHA256_ENV: Final = python_runner.PYTHON_AUTHORITY_SHA256_ENV
PYTHON_TOOLCHAIN_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_TOOLCHAIN_SHA256"
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH_ENV: Final = "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"
DOCKER_SOCKET: Final = docker_authority.DOCKER_SOCKET
NODE_VERSION_FILE: Final = REPO_ROOT / ".node-version"
FRONTEND_PACKAGE_FILE: Final = REPO_ROOT / "frontend/package.json"
GIT_EXECUTABLE: Final = "/usr/bin/git"
DOCKER_EXECUTABLE: Final = "/usr/bin/docker"
COMPOSE_PLUGIN_EXECUTABLE: Final = "/usr/libexec/docker/cli-plugins/docker-compose"
MAKE_EXECUTABLE: Final = "/usr/bin/make"
BOOTSTRAP_PYTHON_EXECUTABLE: Final = "/usr/bin/python3.10"
BROWSER_ROOT: Final = Path("/opt/google/chrome")
BROWSER_EXECUTABLE: Final = BROWSER_ROOT / "chrome"
_SYSTEM_TOOLS: Final = ("awk", "bash", "curl", "docker", "env", "git", "make", "sh", "sleep", "tr")
_COMPOSE_PLUGIN_DIRECTORIES: Final = (
    Path("/usr/local/lib/docker/cli-plugins"),
    Path("/usr/local/libexec/docker/cli-plugins"),
    Path("/usr/lib/docker/cli-plugins"),
    Path("/usr/libexec/docker/cli-plugins"),
)
_NODE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_PNPM_CONTRACT = re.compile(r"^pnpm@([0-9]+\.[0-9]+\.[0-9]+)$")
_SAFE_ENV_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")


class ToolchainAuthorityError(RuntimeError):
    """验收工具、daemon 或依赖偏离固定 authority。"""


ToolAuthority = tool_authority.ToolAuthority
FileAuthority = tool_authority.FileAuthority


class PythonEnvironmentAuthority(TypedDict):
    prefix: str
    version: str
    config: FileAuthority
    dependency_contract_sha256: str
    site_packages: dependency_authority.DependencyTreeAuthority


class ToolchainEvidence(TypedDict):
    contract: str
    node_version: str
    pnpm_version: str
    tools: list[ToolAuthority]
    source_contract_sha256: str
    python_environment: PythonEnvironmentAuthority
    frontend_dependencies: dependency_authority.DependencyTreeAuthority
    pnpm_runtime: dependency_authority.DependencyTreeAuthority
    browser_runtime: dependency_authority.DependencyTreeAuthority
    docker_daemon: docker_authority.DockerDaemonAuthority
    private_state_authority_sha256: str


class LauncherEnvironment(dict[str, str]):
    """仅承载公共验收 bootstrap 允许进入 runner 的环境。"""


class ManagedToolEnvironment(dict[str, str]):
    """由 toolchain authority 生成并写入受管 child 的固定工具变量。"""


class ActualLoadedSourceDigests(dict[str, str]):
    """首阶段实际加载并冻结的仓库源码摘要。"""


@dataclass(frozen=True, slots=True)
class CapturedToolchainAuthority:
    payload: ToolchainEvidence
    sha256: str
    controlled_path: str


def candidate_snapshot_parent_requirement() -> CandidateSnapshotParentRequirement:
    try:
        state = tool_authority.capture_private_state_authority()
    except tool_authority.ToolFileAuthorityError as exc:
        raise ToolchainAuthorityError("candidate snapshot parent authority is unavailable") from exc
    if state.sha256 != _active_authority().payload["private_state_authority_sha256"]:
        raise ToolchainAuthorityError("candidate snapshot parent authority drifted")
    identity = state.candidates
    return CandidateSnapshotParentRequirement(
        Path(identity["path"]),
        identity["device"],
        identity["inode"],
        identity["mode"],
        identity["uid"],
        identity["gid"],
    )


def validate_candidate_snapshot_parent(requirement: CandidateSnapshotParentRequirement) -> None:
    if requirement != candidate_snapshot_parent_requirement():
        raise ToolchainAuthorityError("candidate snapshot parent authority drifted")


def receipt_root_requirement() -> ReceiptRootRequirement:
    try:
        state = tool_authority.capture_private_state_authority()
    except tool_authority.ToolFileAuthorityError as exc:
        raise ToolchainAuthorityError("acceptance receipt root authority is unavailable") from exc
    if state.sha256 != _active_authority().payload["private_state_authority_sha256"]:
        raise ToolchainAuthorityError("acceptance receipt root authority drifted")
    identity = state.receipts
    return ReceiptRootRequirement(
        Path(identity["path"]),
        identity["device"],
        identity["inode"],
        identity["mode"],
        identity["uid"],
        identity["gid"],
    )


def validate_receipt_root(requirement: ReceiptRootRequirement) -> None:
    if requirement != receipt_root_requirement():
        raise ToolchainAuthorityError("acceptance receipt root authority drifted")


def frontend_dependency_projection_requirement() -> FrontendDependencyProjectionRequirement:
    return frontend_requirement(_active_authority().payload["frontend_dependencies"])


def validate_frontend_dependency_projection(requirement: FrontendDependencyProjectionRequirement) -> None:
    try:
        observed = dependency_authority.capture_dependency_tree(requirement.target_root)
    except dependency_authority.DependencyAuthorityError as exc:
        raise ToolchainAuthorityError("frontend dependency projection authority is unavailable") from exc
    expected = frontend_dependency_projection_requirement()
    if requirement != expected or observed != _active_authority().payload["frontend_dependencies"]:
        raise ToolchainAuthorityError("frontend dependency projection authority drifted")


def python_dependency_snapshot_requirement() -> PythonDependencySnapshotRequirement:
    return python_requirement(_active_authority().payload["python_environment"]["site_packages"])


def validate_python_dependency_snapshot(requirement: PythonDependencySnapshotRequirement) -> None:
    try:
        observed = dependency_authority.capture_dependency_tree(requirement.target_root)
    except dependency_authority.DependencyAuthorityError as exc:
        raise ToolchainAuthorityError("Python dependency snapshot authority is unavailable") from exc
    expected = python_dependency_snapshot_requirement()
    if requirement != expected or observed != _active_authority().payload["python_environment"]["site_packages"]:
        raise ToolchainAuthorityError("Python dependency snapshot authority drifted")


def pnpm_dependency_snapshot_requirement() -> PnpmDependencySnapshotRequirement:
    return pnpm_requirement(_active_authority().payload["pnpm_runtime"])


def validate_pnpm_dependency_snapshot(requirement: PnpmDependencySnapshotRequirement) -> None:
    try:
        observed = dependency_authority.capture_dependency_tree(requirement.target_root)
    except dependency_authority.DependencyAuthorityError as exc:
        raise ToolchainAuthorityError("pnpm dependency snapshot authority is unavailable") from exc
    if requirement != pnpm_dependency_snapshot_requirement() or observed != _active_authority().payload["pnpm_runtime"]:
        raise ToolchainAuthorityError("pnpm dependency snapshot authority drifted")


def node_executable_snapshot_requirement() -> NodeExecutableSnapshotRequirement:
    records = tuple(item for item in _active_authority().payload["tools"] if item["command"] == "node")
    if len(records) != 1:
        raise ToolchainAuthorityError("Node executable snapshot authority is unavailable")
    record = records[0]
    if record["leaf_link"] is not None or record["invocation_path"] != record["resolved_path"]:
        raise ToolchainAuthorityError("Node executable snapshot authority is invalid")
    return node_requirement(record)


def validate_node_executable_snapshot(requirement: NodeExecutableSnapshotRequirement) -> None:
    expected = node_executable_snapshot_requirement()
    record = next(item for item in _active_authority().payload["tools"] if item["command"] == "node")
    try:
        descriptor = tool_authority.open_verified_file(record, require_executable=True)
    except tool_authority.ToolFileAuthorityError as exc:
        raise ToolchainAuthorityError("Node executable snapshot authority is unavailable") from exc
    os.close(descriptor)
    if requirement != expected:
        raise ToolchainAuthorityError("Node executable snapshot authority drifted")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _capture_file(path: Path, command: str, **options: bool) -> tuple[ToolAuthority, bytes | None]:
    try:
        return tool_authority.capture_tool_file(path, command, **options)
    except tool_authority.ToolFileAuthorityError as exc:
        raise ToolchainAuthorityError(str(exc)) from exc


def _file_authority(path: Path, label: str, *, candidate_source: bool = False) -> tuple[FileAuthority, bytes]:
    try:
        return tool_authority.capture_small_file(path, label, allow_sticky_ancestor=candidate_source)
    except tool_authority.ToolFileAuthorityError as exc:
        raise ToolchainAuthorityError(str(exc)) from exc


def _portable_contract(label: str, authority: FileAuthority) -> tuple[object, ...]:
    return (label, authority["sha256"], authority["mode"], authority["size"])


def _pinned_node_authority() -> tuple[Path, str, str, tuple[FileAuthority, ...]]:
    node_pin, encoded_pin = _file_authority(NODE_VERSION_FILE, "node-version", candidate_source=True)
    version = encoded_pin.decode("utf-8").strip()
    if _NODE_VERSION.fullmatch(version) is None:
        raise ToolchainAuthorityError("repository Node version pin is invalid")
    package_authority, encoded_package = _file_authority(FRONTEND_PACKAGE_FILE, "frontend-package", candidate_source=True)
    try:
        package = json.loads(encoded_package)
    except (UnicodeError, ValueError) as exc:
        raise ToolchainAuthorityError("frontend package contract is invalid") from exc
    contract = package.get("packageManager") if isinstance(package, dict) else None
    match = _PNPM_CONTRACT.fullmatch(contract) if isinstance(contract, str) else None
    if match is None:
        raise ToolchainAuthorityError("frontend pnpm contract is invalid")
    try:
        home = tool_authority.trusted_home()
    except tool_authority.ToolFileAuthorityError as exc:
        raise ToolchainAuthorityError("process home authority is unavailable") from exc
    candidates = (
        home / f".config/nvm/versions/node/v{version}/bin",
        home / f".nvm/versions/node/v{version}/bin",
    )
    available = tuple(path for path in candidates if path.is_dir() and not path.is_symlink())
    if len(available) != 1:
        raise ToolchainAuthorityError("pinned NVM Node authority is ambiguous or unavailable")
    return available[0], version, match.group(1), (node_pin, package_authority)


def _system_tool(command: str) -> Path:
    located = shutil.which(command, path=os.defpath)
    if located is None:
        raise ToolchainAuthorityError(f"system acceptance tool is unavailable: {command}")
    return Path(located).resolve(strict=True)


def _compose_plugin() -> Path:
    candidates = tuple(directory / "docker-compose" for directory in _COMPOSE_PLUGIN_DIRECTORIES)
    available = tuple(path for path in candidates if path.exists())
    if len(available) != 1:
        raise ToolchainAuthorityError("fixed Docker Compose plugin authority is ambiguous or unavailable")
    return available[0]


DependencyCapturer = Callable[[Path], dependency_authority.DependencyTreeAuthority]
DaemonCapturer = Callable[[], docker_authority.DockerDaemonAuthority]


def _dependency_roots(baseline: CapturedToolchainAuthority | None, nvm_bin: Path) -> tuple[Path, Path, Path]:
    if baseline is None:
        return REPO_ROOT / ".venv", REPO_ROOT / "frontend/node_modules", nvm_bin.parent / "lib/node_modules/pnpm"
    python_root = baseline.payload["python_environment"].get("prefix")
    frontend_root = baseline.payload["frontend_dependencies"].get("root")
    pnpm_root = baseline.payload["pnpm_runtime"].get("root")
    if not isinstance(python_root, str) or not isinstance(frontend_root, str) or not isinstance(pnpm_root, str):
        raise ToolchainAuthorityError("serialized dependency roots are invalid")
    roots = (Path(python_root), Path(frontend_root), Path(pnpm_root))
    if (
        any(not path.is_absolute() for path in roots)
        or roots[0].name != ".venv"
        or roots[1] != roots[0].parent / "frontend/node_modules"
        or roots[2] != nvm_bin.parent / "lib/node_modules/pnpm"
    ):
        raise ToolchainAuthorityError("serialized dependency roots are invalid")
    return roots


def _python_environment(dependency_capturer: DependencyCapturer, prefix: Path) -> tuple[PythonEnvironmentAuthority, tuple[tuple[object, ...], ...]]:
    if Path(os.path.abspath(sys.prefix)) != prefix:
        raise ToolchainAuthorityError("container acceptance did not start from the repository Python")
    config, _encoded_config = _file_authority(prefix / "pyvenv.cfg", "python-environment")
    contracts: list[tuple[object, ...]] = []
    for path in (REPO_ROOT / "requirements.txt", REPO_ROOT / "pyproject.toml", REPO_ROOT / "uv.lock"):
        authority, _encoded = _file_authority(path, "python-dependency-contract", candidate_source=True)
        contracts.append(_portable_contract(path.name, authority))
    site_packages = dependency_capturer(prefix / "lib/python3.11/site-packages")
    return (
        {
            "prefix": str(prefix),
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "config": config,
            "dependency_contract_sha256": hashlib.sha256(_canonical_json(contracts)).hexdigest(),
            "site_packages": site_packages,
        },
        tuple(contracts),
    )


def _capture_toolchain(
    dependency_capturer: DependencyCapturer,
    daemon_capturer: DaemonCapturer,
    baseline: CapturedToolchainAuthority | None,
) -> CapturedToolchainAuthority:
    state_sha256 = tool_authority.capture_private_state_authority().sha256
    nvm_bin, node_version, pnpm_version, node_contracts = _pinned_node_authority()
    python_root, frontend_root, pnpm_root = _dependency_roots(baseline, nvm_bin)
    python_record, _unused = _capture_file(python_root / "bin/python", "python", allow_symlink=True, executable=True)
    bootstrap_python_record, _unused = _capture_file(
        Path(BOOTSTRAP_PYTHON_EXECUTABLE),
        "bootstrap-python",
        executable=True,
    )
    node_record, _unused = _capture_file(nvm_bin / "node", "node", executable=True)
    pnpm_record, _unused = _capture_file(nvm_bin / "pnpm", "pnpm", allow_symlink=True, executable=True)
    expected_pnpm = pnpm_root / "bin/pnpm.cjs"
    if Path(pnpm_record["resolved_path"]) != expected_pnpm:
        raise ToolchainAuthorityError("pnpm link target is outside the pinned NVM version")
    package, encoded_package = _file_authority(expected_pnpm.parents[1] / "package.json", "installed-pnpm")
    installed = json.loads(encoded_package)
    if not isinstance(installed, dict) or installed.get("version") != pnpm_version:
        raise ToolchainAuthorityError("installed pnpm version does not match frontend contract")
    system_records = [_capture_file(_system_tool(command), command, executable=True)[0] for command in _SYSTEM_TOOLS]
    compose_record, _unused = _capture_file(_compose_plugin(), "docker-compose", executable=True)
    browser_record, _unused = _capture_file(BROWSER_EXECUTABLE, "chromium", executable=True)
    python_environment, python_contracts = _python_environment(dependency_capturer, python_root)
    frontend_dependencies = dependency_capturer(frontend_root)
    pnpm_runtime = dependency_capturer(pnpm_root)
    browser_runtime = dependency_capturer(BROWSER_ROOT)
    if browser_runtime["uid"] != 0 or browser_runtime["mode"] != 0o755:
        raise ToolchainAuthorityError("fixed browser runtime authority is invalid")
    source_contracts = (
        *(_portable_contract(path.name, authority) for path, authority in zip((NODE_VERSION_FILE, FRONTEND_PACKAGE_FILE), node_contracts, strict=True)),
        _portable_contract("installed-pnpm", package),
        *python_contracts,
    )
    payload: ToolchainEvidence = {
        "contract": TOOLCHAIN_CONTRACT,
        "node_version": node_version,
        "pnpm_version": pnpm_version,
        "tools": [python_record, bootstrap_python_record, node_record, pnpm_record, compose_record, browser_record, *system_records],
        "source_contract_sha256": hashlib.sha256(_canonical_json(source_contracts)).hexdigest(),
        "python_environment": python_environment,
        "frontend_dependencies": frontend_dependencies,
        "pnpm_runtime": pnpm_runtime,
        "browser_runtime": browser_runtime,
        "docker_daemon": daemon_capturer(),
        "private_state_authority_sha256": state_sha256,
    }
    controlled_path = os.pathsep.join((str(nvm_bin), str(frontend_root / ".bin"), "/usr/bin"))
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return CapturedToolchainAuthority(payload=payload, sha256=digest, controlled_path=controlled_path)


_ACTIVE_AUTHORITY: CapturedToolchainAuthority | None = None


def capture_toolchain_authority(
    *,
    dependency_capturer: DependencyCapturer = dependency_authority.capture_dependency_tree,
    daemon_capturer: DaemonCapturer = docker_authority.capture_docker_daemon_authority,
    baseline: CapturedToolchainAuthority | None = None,
) -> CapturedToolchainAuthority:
    try:
        return _capture_toolchain(dependency_capturer, daemon_capturer, baseline)
    except ToolchainAuthorityError:
        raise
    except (dependency_authority.DependencyAuthorityError, docker_authority.DockerAuthorityError, tool_authority.ToolFileAuthorityError) as exc:
        raise ToolchainAuthorityError("fixed acceptance toolchain authority is unavailable") from exc


def activate_toolchain_authority(authority: CapturedToolchainAuthority) -> None:
    global _ACTIVE_AUTHORITY
    if _ACTIVE_AUTHORITY is not None and authority != _ACTIVE_AUTHORITY:
        raise ToolchainAuthorityError("active acceptance toolchain authority cannot be replaced")
    _ACTIVE_AUTHORITY = authority


def _active_authority() -> CapturedToolchainAuthority:
    if _ACTIVE_AUTHORITY is None:
        raise ToolchainAuthorityError("acceptance toolchain authority was not initialized by the launcher")
    return _ACTIVE_AUTHORITY


def active_toolchain_authority() -> CapturedToolchainAuthority:
    return _active_authority()


def actual_loaded_source_sha256(relative_path: str) -> str:
    registry = globals().get("_ACTUAL_LOADED_SOURCE_REGISTRY")
    lookup = getattr(registry, "digest", None)
    if not callable(lookup):
        raise ToolchainAuthorityError("actual-loaded source registry is unavailable")
    observed = lookup(relative_path)
    if not isinstance(observed, str) or re.fullmatch(r"[0-9a-f]{64}", observed) is None:
        raise ToolchainAuthorityError("actual-loaded source digest is invalid")
    return observed


def freeze_actual_loaded_source_digests() -> ActualLoadedSourceDigests:
    registry = globals().get("_ACTUAL_LOADED_SOURCE_REGISTRY")
    freeze = getattr(registry, "freeze", None)
    if not callable(freeze):
        raise ToolchainAuthorityError("actual-loaded source registry is unavailable")
    observed = freeze()
    if (
        not isinstance(observed, dict)
        or not observed
        or len(observed) > 96
        or any(not isinstance(path, str) or not path.endswith(".py") for path in observed)
        or any(not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in observed.values())
    ):
        raise ToolchainAuthorityError("actual-loaded source registry is invalid")
    return ActualLoadedSourceDigests(observed)


def _active_tool_path(command: str) -> str:
    matches = tuple(item["invocation_path"] for item in _active_authority().payload["tools"] if item["command"] == command)
    if len(matches) != 1:
        raise ToolchainAuthorityError(f"fixed tool path is unavailable: {command}")
    return matches[0]


def _active_tool_sha256(command: str) -> str:
    matches = tuple(item["sha256"] for item in _active_authority().payload["tools"] if item["command"] == command)
    if len(matches) != 1:
        raise ToolchainAuthorityError(f"fixed tool digest is unavailable: {command}")
    return matches[0]


def toolchain_receipt_payload() -> dict[str, object]:
    payload = _active_authority().payload
    python = payload["python_environment"]
    return {
        "contract": payload["contract"],
        "node_version": payload["node_version"],
        "pnpm_version": payload["pnpm_version"],
        "tools": [{"command": item["command"], "authority_sha256": hashlib.sha256(_canonical_json(item)).hexdigest()} for item in payload["tools"]],
        "source_contract_sha256": payload["source_contract_sha256"],
        "python_environment": {
            "version": python["version"],
            "config_sha256": python["config"]["sha256"],
            "dependency_contract_sha256": python["dependency_contract_sha256"],
            "site_packages": _redacted_dependency_payload(python["site_packages"]),
        },
        "frontend_dependencies": _redacted_dependency_payload(payload["frontend_dependencies"]),
        "pnpm_runtime": _redacted_dependency_payload(payload["pnpm_runtime"]),
        "browser_runtime": _redacted_dependency_payload(payload["browser_runtime"]),
        "docker_daemon": dict(payload["docker_daemon"]),
        "private_state_authority_sha256": payload["private_state_authority_sha256"],
    }


def _redacted_dependency_payload(value: dependency_authority.DependencyTreeAuthority) -> dict[str, int | str]:
    return {key: value[key] for key in ("entries", "regular_bytes", "sha256", "projection_sha256")}


def toolchain_sha256() -> str:
    return _active_authority().sha256


def controlled_path(
    frontend_dependency_root: Path | None = None,
    pnpm_dependency_root: Path | None = None,
) -> str:
    if frontend_dependency_root is None:
        return _active_authority().controlled_path
    root = Path(frontend_dependency_root)
    node = Path(_managed_node_path(pnpm_dependency_root)).parent
    if not root.is_absolute() or root.name != "node_modules" or root.parent.name != "frontend":
        raise ToolchainAuthorityError("prepared frontend dependency root is invalid")
    return os.pathsep.join((str(node), str(root / ".bin"), "/usr/bin"))


def _validate_serialized_payload(payload: object) -> ToolchainEvidence:
    if not isinstance(payload, dict) or set(payload) != {
        "contract",
        "node_version",
        "pnpm_version",
        "tools",
        "source_contract_sha256",
        "python_environment",
        "frontend_dependencies",
        "pnpm_runtime",
        "browser_runtime",
        "docker_daemon",
        "private_state_authority_sha256",
    }:
        raise ToolchainAuthorityError("serialized acceptance toolchain evidence is invalid")
    tools = payload.get("tools")
    digests = (payload.get("source_contract_sha256"), payload.get("private_state_authority_sha256"))
    if (
        payload.get("contract") != TOOLCHAIN_CONTRACT
        or not isinstance(tools, list)
        or not tools
        or any(not isinstance(item, dict) or not isinstance(item.get("command"), str) for item in tools)
        or any(not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in tools)
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in digests)
    ):
        raise ToolchainAuthorityError("serialized acceptance toolchain evidence is invalid")
    return cast(ToolchainEvidence, payload)


def serialized_authority_environment(authority: CapturedToolchainAuthority) -> LauncherEnvironment:
    return LauncherEnvironment(
        {
            TOOLCHAIN_EVIDENCE_ENV: _canonical_json(authority.payload).decode("utf-8"),
            TOOLCHAIN_SHA256_ENV: authority.sha256,
        }
    )


def initialize_toolchain_authority(environ: Mapping[str, str]) -> CapturedToolchainAuthority:
    if _ACTIVE_AUTHORITY is not None:
        return _ACTIVE_AUTHORITY
    encoded = environ.get(TOOLCHAIN_EVIDENCE_ENV)
    expected_digest = environ.get(TOOLCHAIN_SHA256_ENV)
    if not encoded or not expected_digest or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ToolchainAuthorityError("serialized acceptance toolchain authority is missing")
    try:
        payload = _validate_serialized_payload(json.loads(encoded))
    except (UnicodeError, ValueError) as exc:
        raise ToolchainAuthorityError("serialized acceptance toolchain authority is invalid") from exc
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if digest != expected_digest:
        raise ToolchainAuthorityError("serialized acceptance toolchain digest is invalid")
    node_paths = tuple(item["invocation_path"] for item in payload["tools"] if item["command"] == "node")
    frontend_root = payload["frontend_dependencies"].get("root")
    if len(node_paths) != 1 or not isinstance(frontend_root, str) or not Path(frontend_root).is_absolute():
        raise ToolchainAuthorityError("serialized dependency authority is invalid")
    path = os.pathsep.join((str(Path(node_paths[0]).parent), str(Path(frontend_root) / ".bin"), "/usr/bin"))
    authority = CapturedToolchainAuthority(payload, digest, path)
    activate_toolchain_authority(authority)
    return authority


def _managed_pnpm_path(pnpm_dependency_root: Path | None) -> str:
    if pnpm_dependency_root is None:
        return _active_tool_path("pnpm")
    root = Path(pnpm_dependency_root)
    if not root.is_absolute() or root.name != "pnpm" or root.parent.name != "dependencies":
        raise ToolchainAuthorityError("prepared pnpm dependency root is invalid")
    return str(root / "bin/pnpm.cjs")


def _managed_node_path(pnpm_dependency_root: Path | None) -> str:
    if pnpm_dependency_root is None:
        return _active_tool_path("node")
    root = Path(pnpm_dependency_root)
    if not root.is_absolute() or root.name != "pnpm" or root.parent.name != "dependencies":
        raise ToolchainAuthorityError("prepared Node dependency root is invalid")
    return str(root.parent / "node/bin/node")


def managed_tool_environment(
    *,
    frontend_dependency_root: Path | None = None,
    pnpm_dependency_root: Path | None = None,
) -> ManagedToolEnvironment:
    try:
        paths = tool_authority.private_state_paths()
    except tool_authority.ToolFileAuthorityError as exc:
        raise ToolchainAuthorityError("acceptance private state authority is unavailable") from exc
    python_records = tuple(item for item in _active_authority().payload["tools"] if item["command"] == "python")
    if len(python_records) != 1:
        raise ToolchainAuthorityError("fixed Python authority is unavailable")
    python_authority = _canonical_json(python_records[0])
    repository = REPO_ROOT if frontend_dependency_root is None else Path(frontend_dependency_root).parents[1]
    snapshot_toolchain, _encoded = _file_authority(
        repository / "scripts/container_acceptance_toolchain.py",
        "candidate-python-toolchain",
        candidate_source=True,
    )
    return ManagedToolEnvironment(
        {
            "PATH": controlled_path(frontend_dependency_root, pnpm_dependency_root),
            "DOCKER_HOST": f"unix://{DOCKER_SOCKET}",
            "DOCKER_CONFIG": str(paths.docker_config),
            "DOCKER_CLI_PLUGIN_EXTRA_DIRS": str(Path(COMPOSE_PLUGIN_EXECUTABLE).parent),
            PYTHON_EXECUTABLE_ENV: _active_tool_path("python"),
            NODE_EXECUTABLE_ENV: _managed_node_path(pnpm_dependency_root),
            PNPM_EXECUTABLE_ENV: _managed_pnpm_path(pnpm_dependency_root),
            GIT_EXECUTABLE_ENV: _active_tool_path("git"),
            DOCKER_EXECUTABLE_ENV: _active_tool_path("docker"),
            COMPOSE_PLUGIN_ENV: _active_tool_path("docker-compose"),
            MAKE_EXECUTABLE_ENV: _active_tool_path("make"),
            PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH_ENV: _active_tool_path("chromium"),
            PYTHON_AUTHORITY_ENV: python_authority.decode(),
            PYTHON_AUTHORITY_SHA256_ENV: hashlib.sha256(python_authority).hexdigest(),
            PYTHON_TOOLCHAIN_SHA256_ENV: snapshot_toolchain["sha256"],
            SYSTEM_PYTHON_SHA256_ENV: _active_tool_sha256("bootstrap-python"),
        }
    )


def validate_toolchain_authority(environ: Mapping[str, str] | None = None) -> None:
    observed = capture_toolchain_authority(baseline=_active_authority())
    if observed != _active_authority():
        raise ToolchainAuthorityError("acceptance toolchain authority drifted")
    if environ is not None:
        frontend_root = environ.get(FRONTEND_DEPENDENCY_ROOT_ENV)
        pnpm_root = environ.get(PNPM_DEPENDENCY_ROOT_ENV)
        expected = managed_tool_environment(
            frontend_dependency_root=Path(frontend_root) if isinstance(frontend_root, str) else None,
            pnpm_dependency_root=Path(pnpm_root) if isinstance(pnpm_root, str) else None,
        )
        if any(environ.get(key) != value for key, value in expected.items()):
            raise ToolchainAuthorityError("managed acceptance tool environment drifted")


def validate_execution_tool_authority(
    environ: Mapping[str, str] | None = None,
    *,
    commands: tuple[str, ...] | None = None,
) -> None:
    selected = set(commands or (item["command"] for item in _active_authority().payload["tools"] if item["command"] != "chromium"))
    records = tuple(item for item in _active_authority().payload["tools"] if item["command"] in selected)
    if len(records) != len(selected):
        raise ToolchainAuthorityError("fixed execution tool selection is invalid")
    try:
        for record in records:
            descriptor = tool_authority.open_verified_file(record, require_executable=True)
            os.close(descriptor)
        state = tool_authority.capture_private_state_authority()
        daemon = docker_authority.capture_docker_daemon_authority()
    except (tool_authority.ToolFileAuthorityError, docker_authority.DockerAuthorityError) as exc:
        raise ToolchainAuthorityError("fixed execution tool authority is unavailable") from exc
    if state.sha256 != _active_authority().payload["private_state_authority_sha256"] or daemon != _active_authority().payload["docker_daemon"]:
        raise ToolchainAuthorityError("fixed execution tool authority drifted")
    if environ is not None:
        frontend_root = environ.get(FRONTEND_DEPENDENCY_ROOT_ENV)
        pnpm_root = environ.get(PNPM_DEPENDENCY_ROOT_ENV)
        expected = managed_tool_environment(
            frontend_dependency_root=Path(frontend_root) if isinstance(frontend_root, str) else None,
            pnpm_dependency_root=Path(pnpm_root) if isinstance(pnpm_root, str) else None,
        )
        if any(environ.get(key) != value for key, value in expected.items()):
            raise ToolchainAuthorityError("managed execution tool environment drifted")


def validate_browser_runtime_authority() -> None:
    """Re-hash the fixed browser immediately around a managed verifier."""

    validate_execution_tool_authority(commands=("chromium",))
    try:
        observed = dependency_authority.capture_dependency_tree(BROWSER_ROOT)
    except dependency_authority.DependencyAuthorityError as exc:
        raise ToolchainAuthorityError("fixed browser runtime authority is unavailable") from exc
    if observed != _active_authority().payload["browser_runtime"]:
        raise ToolchainAuthorityError("fixed browser runtime authority drifted")


def validate_dependency_tree_generations() -> None:
    payload = _active_authority().payload
    trees = (
        payload["python_environment"]["site_packages"],
        payload["frontend_dependencies"],
        payload["pnpm_runtime"],
        payload["browser_runtime"],
    )
    try:
        for tree in trees:
            dependency_authority.require_dependency_generation_current(tree)
    except dependency_authority.DependencyAuthorityError as exc:
        raise ToolchainAuthorityError("fixed dependency generation authority drifted") from exc


def git_argv(repository: Path, *arguments: str) -> tuple[str, ...]:
    source_root = Path(_active_authority().payload["python_environment"]["prefix"]).parent
    try:
        return git_authority.git_argv(repository, arguments, git_executable=_active_tool_path("git"), source_root=source_root)
    except git_authority.GitAuthorityError as exc:
        raise ToolchainAuthorityError(str(exc)) from exc


def git_environment(*, index_file: Path | None = None) -> git_authority.GitEnvironment:
    try:
        return git_authority.git_environment(index_file=index_file)
    except git_authority.GitAuthorityError as exc:
        raise ToolchainAuthorityError(str(exc)) from exc


def selected_env_path(environ: Mapping[str, str]) -> Path:
    raw = environ.get("COMPOSE_ENV_FILE")
    if not raw or _SAFE_ENV_PATH.fullmatch(raw) is None:
        raise ToolchainAuthorityError("selected Compose env path is invalid")
    selected = Path(raw) if Path(raw).is_absolute() else REPO_ROOT / raw
    authority, _encoded = _file_authority(selected, "selected-compose-env")
    return Path(authority["path"])


def exec_authoritative_python(
    entrypoint: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
) -> NoReturn:
    python_records = tuple(item for item in _active_authority().payload["tools"] if item["command"] == "python")
    if len(python_records) != 1:
        raise ToolchainAuthorityError("fixed Python executable authority is unavailable")
    try:
        snapshot_exec.exec_snapshot_python(
            entrypoint,
            arguments,
            environment,
            python_record=python_records[0],
        )
    except snapshot_exec.SnapshotExecAuthorityError as exc:
        raise ToolchainAuthorityError("candidate acceptance runner authority drifted") from exc


def _launch_runner(arguments: list[str], environ: Mapping[str, str]) -> None:
    from scripts import container_acceptance_launcher_entry as launcher_entry

    try:
        launcher_entry.launch(arguments, environ)
    except ToolchainAuthorityError:
        raise
    except Exception as exc:
        raise ToolchainAuthorityError("reserved acceptance launcher orchestration failed") from exc


def _run_python(arguments: list[str]) -> None:
    try:
        python_runner.run(arguments, REPO_ROOT, os.environ)
    except python_runner.PythonRunnerAuthorityError as exc:
        raise ToolchainAuthorityError("fixed acceptance Python target is invalid") from exc


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        mode = arguments.pop(0)
        if mode == "launch":
            _launch_runner(arguments, os.environ)
        elif mode == "python":
            _run_python(arguments)
        else:
            raise ToolchainAuthorityError("container acceptance toolchain mode is invalid")
    except (OSError, UnicodeError, ValueError, ToolchainAuthorityError):
        print("CONTAINER_ACCEPTANCE_LAUNCHER_FAIL: fixed launcher authority is invalid", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
