"""容器验收受管环境与 fd-authoritative 隔离运行根。"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from app.agent_testing.docker_volume_contracts import (
    SANDBOX_KIND_LABEL,
    SANDBOX_SCOPE_LABEL,
    validate_docker_volume_name,
    validated_local_volume_mountpoint,
)
from scripts import agent_test_acceptance_support as acceptance_support
from scripts import container_acceptance_candidate as acceptance_candidate
from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_contract as acceptance_contract
from scripts import container_acceptance_receipt_authority as receipt_authority
from scripts import container_acceptance_tool_authority as tool_authority

RUNTIME_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT"
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_CLEANUP_ENTRIES: Final = 100_000
_MAX_CLEANUP_DEPTH: Final = 64
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_AGENT_TEST_RUNS_VOLUME: Final = "agent-test-runs"
_COMPOSE_PROJECT_LABEL: Final = "com.docker.compose.project"
_COMPOSE_VOLUME_LABEL: Final = "com.docker.compose.volume"
_COMPOSE_STOP_TIMEOUT_SECONDS: Final = "5"


class AcceptanceEnvironmentError(RuntimeError):
    """受管环境或隔离临时根不满足验收 authority。"""


class IsolatedRuntimeEnvironment(dict[str, str]):
    """仅承载 runner 生成并由回执摘要绑定的隔离运行变量。"""


class AcceptanceProfileLike(Protocol):
    name: str
    isolated_runtime: bool


class CleanupCommandRunner(Protocol):
    def __call__(self, command: list[str], *, capture: bool = False) -> str: ...


@dataclass(slots=True)
class IsolatedRuntimeAuthority:
    path: Path
    snapshot_parent: Path
    snapshot_parent_device: int
    snapshot_parent_inode: int
    snapshot_parent_descriptor: int
    parent_descriptor: int
    root_descriptor: int
    parent_device: int
    parent_inode: int
    root_device: int
    root_inode: int

    def assert_current(self) -> None:
        _require_current_parent(self)
        _require_linked_directory(
            self.parent_descriptor,
            self.path.name,
            device=self.root_device,
            inode=self.root_inode,
        )

    def cleanup(self) -> None:
        try:
            self.assert_current()
            _clear_directory(
                self.root_descriptor,
                expected_device=self.root_device,
                budget=_CleanupBudget(),
                depth=0,
            )
            self.assert_current()
            _require_empty_root(self)
        except OSError as exc:
            raise AcceptanceEnvironmentError("候选验收运行目录未完全清理，已保留 residue") from exc
        finally:
            self.close()

    def close(self) -> None:
        for descriptor in (self.root_descriptor, self.parent_descriptor, self.snapshot_parent_descriptor):
            with suppress(OSError):
                os.close(descriptor)


@dataclass(slots=True)
class _CleanupBudget:
    remaining: int = field(default_factory=lambda: _MAX_CLEANUP_ENTRIES)


@dataclass(frozen=True, slots=True)
class _NamedVolumeEvidence:
    name: str
    mountpoint: Path
    scope_id: str


def _runtime_token(run_id: str) -> str:
    token = "".join(character for character in run_id.lower() if character.isalnum())[-20:]
    if len(token) < 12:
        raise AcceptanceEnvironmentError("隔离验收 run identity 无效")
    return token


def _isolated_runtime_environment(
    profile: AcceptanceProfileLike,
    runtime_root: Path,
    run_id: str,
    *,
    available_port: Callable[[], int],
) -> IsolatedRuntimeEnvironment:
    token = _runtime_token(run_id)
    project_name = f"agentgov-acceptance-{token}"
    api_port = available_port()
    volume_root = runtime_root / "volumes"
    values = IsolatedRuntimeEnvironment(
        {
            "COMPOSE_PROJECT_NAME": project_name,
            "CONTAINER_NAME_PREFIX": project_name,
            "HOST_RUNTIME_VOLUME_ROOT": str(volume_root),
            "HOST_DATA_MOUNT": str(volume_root / "data"),
            "HOST_GOVERNOR_WORKSPACE_MOUNT": str(volume_root / "governor-workspace"),
            "HOST_GOVERNOR_CLAUDE_ROOT_MOUNT": str(volume_root / "claude-roots" / "governor"),
            "HOST_PORT": str(api_port),
            "API_BASE": f"http://127.0.0.1:{api_port}",
            "AGENT_TEST_RUN_TIMEOUT_SECONDS": "10",
            "AGENT_TEST_WORKER_POLL_SECONDS": "0.1",
        }
    )
    if profile.name == "isolated-health":
        _add_health_environment(values, run_id, api_port, available_port=available_port)
    return values


def _add_health_environment(
    values: IsolatedRuntimeEnvironment,
    run_id: str,
    api_port: int,
    *,
    available_port: Callable[[], int],
) -> None:
    ui_port = available_port()
    if ui_port == api_port:
        ui_port = available_port()
    api_key = f"agentgov-health-{secrets.token_hex(16)}"
    values.update(
        {
            "API_KEY": api_key,
            "FRONTEND_HOST_PORT": str(ui_port),
            "FRONTEND_RUNTIME_API_BASE": f"http://localhost:{api_port}",
            "FRONTEND_RUNTIME_API_KEY": api_key,
        }
    )


def build_acceptance_env(
    profile: AcceptanceProfileLike,
    candidate: candidate_authority.PreparedCandidateAuthority,
    runtime: IsolatedRuntimeAuthority,
    environ: Mapping[str, str],
    *,
    available_port: Callable[[], int],
) -> acceptance_contract.ManagedAcceptanceEnvironment:
    runtime.assert_current()
    if runtime.path != candidate.runtime_root:
        raise AcceptanceEnvironmentError("候选验收运行目录与源码快照不一致")
    run_id = candidate.snapshot.run_id
    runtime_paths = acceptance_contract.candidate_runtime_environment_paths(runtime.path)
    managed_values = {
        **runtime_paths.managed_values(),
        **_dependency_environment(candidate),
        "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE": "1",
        "AGENT_GOV_ACCEPTANCE_RUN_ID": run_id,
        "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE": candidate.snapshot.profile,
        "COMPOSE_ENV_FILE": str(candidate.env_file),
        "AGENT_GOV_COMPOSE_ENV_FILE": str(candidate.env_file),
        "APP_VERSION": _read_snapshot_version(candidate),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        acceptance_support.ACCEPTANCE_CANDIDATE_TREE_ENV: candidate.snapshot.git_tree_sha,
        acceptance_support.ACCEPTANCE_ENV_DIGEST_ENV: candidate.snapshot.selected_env_sha256,
    }
    if profile.name != candidate.snapshot.profile:
        raise AcceptanceEnvironmentError("候选验收 profile identity 不一致")
    if profile.isolated_runtime:
        managed_values.update(_isolated_runtime_environment(profile, runtime.path, run_id, available_port=available_port))
    else:
        managed_values.update(_persistent_volume_environment(profile.name))
    try:
        return acceptance_contract.build_managed_environment(environ, managed_values=managed_values)
    except acceptance_contract.AcceptanceContractError as exc:
        raise AcceptanceEnvironmentError("无法构造受管容器验收环境") from exc


def _dependency_environment(
    candidate: candidate_authority.PreparedCandidateAuthority,
) -> IsolatedRuntimeEnvironment:
    frontend = candidate.snapshot.frontend_dependencies
    python = candidate.snapshot.python_dependencies
    pnpm = candidate.snapshot.pnpm_dependencies
    return IsolatedRuntimeEnvironment(
        {
            acceptance_contract.acceptance_toolchain.FRONTEND_DEPENDENCY_ROOT_ENV: str(frontend.root),
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_SHA256": frontend.sha256,
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_ENTRIES": str(frontend.entries),
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_BYTES": str(frontend.regular_bytes),
            acceptance_contract.acceptance_toolchain.PYTHON_SITE_PACKAGES_ENV: str(python.root),
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_SHA256": python.sha256,
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_ENTRIES": str(python.entries),
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_BYTES": str(python.regular_bytes),
            acceptance_contract.acceptance_toolchain.PNPM_DEPENDENCY_ROOT_ENV: str(pnpm.root),
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_SHA256": pnpm.sha256,
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_ENTRIES": str(pnpm.entries),
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_BYTES": str(pnpm.regular_bytes),
        }
    )


def _read_snapshot_version(candidate: candidate_authority.PreparedCandidateAuthority) -> str:
    relative = PurePosixPath("VERSION")
    acceptance_candidate.require_snapshot_loaded_file(candidate, candidate.snapshot_repository_root / relative, relative)
    expected = {item.relative_path: item.sha256 for item in candidate.snapshot.loaded_sources}.get(relative.as_posix())
    descriptor = os.open(candidate.snapshot_repository_root / relative, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        encoded = os.read(descriptor, 129)
        after = os.fstat(descriptor)
        linked = os.stat(candidate.snapshot_repository_root / relative, follow_symlinks=False)
    finally:
        os.close(descriptor)
    valid = (
        stat.S_ISREG(before.st_mode)
        and len(encoded) <= 128
        and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        and (after.st_dev, after.st_ino) == (linked.st_dev, linked.st_ino)
        and hashlib.sha256(encoded).hexdigest() == expected
    )
    try:
        version = encoded.decode("utf-8").strip()
    except UnicodeError as exc:
        raise AcceptanceEnvironmentError("候选 VERSION authority 无效") from exc
    if not valid or not version or len(version) > 64 or any(ord(character) < 33 for character in version):
        raise AcceptanceEnvironmentError("候选 VERSION authority 无效")
    return version


def _persistent_volume_environment(profile_name: str) -> IsolatedRuntimeEnvironment:
    try:
        root = tool_authority.trusted_home() / "volume-agent-gov"
    except tool_authority.ToolFileAuthorityError as exc:
        raise AcceptanceEnvironmentError("持久卷 pwd-home authority 无效") from exc
    values = IsolatedRuntimeEnvironment(
        {
            "HOST_RUNTIME_VOLUME_ROOT": str(root),
            "HOST_DATA_MOUNT": str(root / "data"),
            "HOST_GOVERNOR_WORKSPACE_MOUNT": str(root / "governor-workspace"),
            "HOST_GOVERNOR_CLAUDE_ROOT_MOUNT": str(root / "claude-roots/governor"),
        }
    )
    if profile_name == "langfuse":
        values.update(
            {
                "LANGFUSE_POSTGRES_DATA_MOUNT": str(root / "langfuse/postgres"),
                "LANGFUSE_CLICKHOUSE_DATA_MOUNT": str(root / "langfuse/clickhouse/data"),
                "LANGFUSE_CLICKHOUSE_LOGS_MOUNT": str(root / "langfuse/clickhouse/logs"),
                "LANGFUSE_REDIS_DATA_MOUNT": str(root / "langfuse/redis"),
                "LANGFUSE_MINIO_DATA_MOUNT": str(root / "langfuse/minio"),
            }
        )
    return values


def _require_linked_directory(directory_fd: int, leaf: str, *, device: int, inode: int) -> os.stat_result:
    observed = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (device, inode):
        raise AcceptanceEnvironmentError("隔离验收运行目录 identity 已变化，已保留 residue")
    return observed


def _create_directory_at(directory_fd: int, leaf: str, *, mode: int) -> int:
    os.mkdir(leaf, mode=mode, dir_fd=directory_fd)
    descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, mode)
        identity = os.fstat(descriptor)
        _require_linked_directory(directory_fd, leaf, device=identity.st_dev, inode=identity.st_ino)
        if identity.st_uid != os.geteuid() or stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
            raise AcceptanceEnvironmentError("隔离验收运行目录 owner/mode 无效")
        os.fsync(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_runtime_children(authority: IsolatedRuntimeAuthority, profile: AcceptanceProfileLike) -> None:
    descriptors: list[int] = []
    try:
        for leaf in ("home", "xdg-config", "buildx", "tmp", "screenshots"):
            descriptors.append(_create_directory_at(authority.root_descriptor, leaf, mode=0o700))
        if profile.isolated_runtime:
            volumes = _create_directory_at(authority.root_descriptor, "volumes", mode=0o755)
            descriptors.append(volumes)
            for leaf in ("data", "governor-workspace"):
                descriptors.append(_create_directory_at(volumes, leaf, mode=0o755))
            claude_roots = _create_directory_at(volumes, "claude-roots", mode=0o755)
            descriptors.append(claude_roots)
            descriptors.append(_create_directory_at(claude_roots, "governor", mode=0o755))
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_runtime_authority(
    snapshot: candidate_authority.CandidateSnapshotIdentity,
) -> IsolatedRuntimeAuthority:
    snapshot_parent_fd = os.open(snapshot.parent, _DIRECTORY_FLAGS)
    parent_fd: int | None = None
    root_fd: int | None = None
    try:
        _require_exact_directory(snapshot_parent_fd, snapshot.parent_identity, "候选快照父目录")
        parent_fd = os.open(snapshot.root.name, _DIRECTORY_FLAGS, dir_fd=snapshot_parent_fd)
        _require_exact_directory(parent_fd, snapshot.root_identity, "候选快照根")
        _require_linked_directory(snapshot_parent_fd, snapshot.root.name, device=snapshot.root_identity.device, inode=snapshot.root_identity.inode)
        root_fd = os.open(snapshot.runtime_root.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        _require_exact_directory(root_fd, snapshot.runtime_identity, "候选验收运行目录")
        root = os.fstat(root_fd)
        authority = IsolatedRuntimeAuthority(
            snapshot.runtime_root,
            snapshot.parent,
            snapshot.parent_identity.device,
            snapshot.parent_identity.inode,
            snapshot_parent_fd,
            parent_fd,
            root_fd,
            snapshot.root_identity.device,
            snapshot.root_identity.inode,
            root.st_dev,
            root.st_ino,
        )
        authority.assert_current()
        if root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o700:
            raise AcceptanceEnvironmentError("候选验收运行目录 owner/mode 无效")
        return authority
    except BaseException:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(snapshot_parent_fd)
        raise


def _runtime_directory_names(descriptor: int, *, maximum: int = 16) -> set[str]:
    names: set[str] = set()
    with os.scandir(descriptor) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > maximum or entry.name in names:
                raise AcceptanceEnvironmentError("候选验收运行目录布局无效")
            names.add(entry.name)
    return names


def _verify_runtime_child(parent_fd: int, leaf: str, *, mode: int) -> int:
    descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    identity = os.fstat(descriptor)
    linked = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(identity.st_mode)
        or (identity.st_dev, identity.st_ino) != (linked.st_dev, linked.st_ino)
        or identity.st_uid != os.geteuid()
        or stat.S_IMODE(identity.st_mode) != mode
    ):
        os.close(descriptor)
        raise AcceptanceEnvironmentError("候选验收运行目录布局无效")
    return descriptor


def _verify_runtime_children(authority: IsolatedRuntimeAuthority, profile: AcceptanceProfileLike) -> None:
    common = {"home", "xdg-config", "buildx", "tmp", "screenshots"}
    expected = common | ({"volumes"} if profile.isolated_runtime else set())
    if _runtime_directory_names(authority.root_descriptor) != expected:
        raise AcceptanceEnvironmentError("候选验收运行目录布局无效")
    descriptors: list[int] = []
    try:
        for leaf in common:
            descriptors.append(_verify_runtime_child(authority.root_descriptor, leaf, mode=0o700))
        if profile.isolated_runtime:
            volumes = _verify_runtime_child(authority.root_descriptor, "volumes", mode=0o755)
            descriptors.append(volumes)
            if _runtime_directory_names(volumes) != {"data", "governor-workspace", "claude-roots"}:
                raise AcceptanceEnvironmentError("候选验收 volume 布局无效")
            for leaf in ("data", "governor-workspace"):
                descriptors.append(_verify_runtime_child(volumes, leaf, mode=0o755))
            claude = _verify_runtime_child(volumes, "claude-roots", mode=0o755)
            descriptors.append(claude)
            if _runtime_directory_names(claude) != {"governor"}:
                raise AcceptanceEnvironmentError("候选验收 Claude root 布局无效")
            descriptors.append(_verify_runtime_child(claude, "governor", mode=0o755))
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def open_prepared_runtime(
    candidate: candidate_authority.PreparedCandidateAuthority,
    profile: AcceptanceProfileLike,
) -> IsolatedRuntimeAuthority:
    acceptance_candidate.verify_candidate_snapshot(candidate)
    authority = _open_runtime_authority(candidate.snapshot)
    try:
        _verify_runtime_children(authority, profile)
        return authority
    except BaseException:
        authority.close()
        raise


def prepare_isolated_runtime(
    candidate: candidate_authority.PreparedCandidateAuthority,
    profile: AcceptanceProfileLike,
) -> IsolatedRuntimeAuthority:
    acceptance_candidate.verify_candidate_snapshot(candidate)
    authority = _open_runtime_authority(candidate.snapshot)
    try:
        _create_runtime_children(authority, profile)
        _verify_runtime_children(authority, profile)
        os.fsync(authority.root_descriptor)
        return authority
    except BaseException as exc:
        try:
            authority.cleanup()
        except BaseException as cleanup_exc:
            raise AcceptanceEnvironmentError("候选验收运行目录准备失败且存在 residue") from cleanup_exc
        if isinstance(exc, AcceptanceEnvironmentError):
            raise
        raise AcceptanceEnvironmentError("候选验收运行目录准备失败") from exc


def _require_exact_directory(descriptor: int, expected: candidate_authority.CandidatePathIdentity, label: str) -> None:
    observed = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (expected.device, expected.inode) or observed.st_uid != os.geteuid():
        raise AcceptanceEnvironmentError(f"{label} authority 无效")


def _resolve_runs_volume(compose_base: list[str], project_name: str, runner: CleanupCommandRunner) -> str:
    raw = runner([*compose_base, "config", "--format", "json"], capture=True)
    try:
        config = acceptance_support.json_value(raw, operation="Compose volume cleanup model")
    except acceptance_support.AcceptanceSupportError as exc:
        raise AcceptanceEnvironmentError("Compose volume cleanup model is invalid") from exc
    volumes = config.get("volumes") if isinstance(config, dict) else None
    logical = volumes.get(_AGENT_TEST_RUNS_VOLUME) if isinstance(volumes, dict) else None
    if not isinstance(config, dict) or config.get("name") != project_name or not isinstance(logical, dict) or logical.get("driver") != "local":
        raise AcceptanceEnvironmentError("Compose agent-test-runs cleanup authority is invalid")
    try:
        return validate_docker_volume_name(logical.get("name"))
    except ValueError as exc:
        raise AcceptanceEnvironmentError("Compose agent-test-runs volume identity is invalid") from exc


def _volume_exists(volume_name: str, runner: CleanupCommandRunner) -> bool:
    output = runner(["docker", "volume", "ls", "--quiet"], capture=True)
    names = tuple(line.strip() for line in output.splitlines() if line.strip())
    return volume_name in names


def _inspect_runs_volume(
    volume_name: str,
    *,
    project_name: str,
    runner: CleanupCommandRunner,
) -> _NamedVolumeEvidence:
    raw = runner(["docker", "volume", "inspect", volume_name], capture=True)
    payload = acceptance_support.json_value(raw, operation="agent-test-runs cleanup inspect")
    volume = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
    labels = volume.get("Labels") if isinstance(volume, dict) else None
    valid_labels = (
        isinstance(labels, dict) and labels.get(_COMPOSE_PROJECT_LABEL) == project_name and labels.get(_COMPOSE_VOLUME_LABEL) == _AGENT_TEST_RUNS_VOLUME
    )
    if not isinstance(volume, dict) or not valid_labels:
        raise AcceptanceEnvironmentError("agent-test-runs cleanup volume is outside this project")
    try:
        mountpoint = validated_local_volume_mountpoint(volume, expected_name=volume_name)  # type: ignore[arg-type]
    except ValueError as exc:
        raise AcceptanceEnvironmentError("agent-test-runs cleanup volume contract is invalid") from exc
    return _NamedVolumeEvidence(volume_name, mountpoint, acceptance_support.sandbox_scope_id(volume_name))


def _volume_user_ids(volume_name: str, runner: CleanupCommandRunner) -> tuple[str, ...]:
    output = runner(
        ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", f"volume={volume_name}"],
        capture=True,
    )
    ids = tuple(line.strip() for line in output.splitlines() if line.strip())
    if len(ids) != len(set(ids)) or any(_CONTAINER_ID.fullmatch(container_id) is None for container_id in ids):
        raise AcceptanceEnvironmentError("agent-test-runs cleanup user identity is invalid")
    return ids


def _inspect_volume_user(container_id: str, volume: _NamedVolumeEvidence, runner: CleanupCommandRunner) -> None:
    raw = runner(["docker", "inspect", container_id], capture=True)
    payload = acceptance_support.json_value(raw, operation="sandbox cleanup inspect")
    inspect = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
    config = inspect.get("Config") if isinstance(inspect, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    mounts = inspect.get("Mounts") if isinstance(inspect, dict) else None
    mount = mounts[0] if isinstance(mounts, list) and len(mounts) == 1 else None
    trusted_labels = isinstance(labels, dict) and labels.get(SANDBOX_KIND_LABEL) == "true" and labels.get(SANDBOX_SCOPE_LABEL) == volume.scope_id
    trusted_mount = (
        isinstance(mount, dict)
        and mount.get("Type") == "volume"
        and mount.get("Name") == volume.name
        and mount.get("Source") == str(volume.mountpoint)
        and mount.get("Destination") == "/workspace"
        and mount.get("Driver") == "local"
        and mount.get("RW") is False
    )
    if not isinstance(inspect, dict) or inspect.get("Id") != container_id or not trusted_labels or not trusted_mount:
        raise AcceptanceEnvironmentError("agent-test-runs cleanup user is outside this scope")


def _remove_volume_users(volume: _NamedVolumeEvidence, runner: CleanupCommandRunner) -> None:
    container_ids = _volume_user_ids(volume.name, runner)
    for container_id in container_ids:
        _inspect_volume_user(container_id, volume, runner)
    for container_id in container_ids:
        runner(["docker", "rm", "--force", container_id])
    if _volume_user_ids(volume.name, runner):
        raise AcceptanceEnvironmentError("agent-test-runs cleanup users remain")


def _cleanup_agent_test_volume(
    compose_base: list[str],
    project_name: str,
    runner: CleanupCommandRunner,
) -> bool:
    volume_name = _resolve_runs_volume(compose_base, project_name, runner)
    if _volume_exists(volume_name, runner):
        volume = _inspect_runs_volume(volume_name, project_name=project_name, runner=runner)
        _remove_volume_users(volume, runner)
    return True


def _cleanup_compose_residue(
    compose_base: list[str],
    *,
    project_name: str,
    run_id: str,
    expected_services: tuple[str, ...],
    expect_runs_volume: bool,
    runner: CleanupCommandRunner,
) -> None:
    config = runner([*compose_base, "config", "--format", "json"], capture=True)
    try:
        acceptance_support.cleanup_isolated_compose_residue(
            compose_config=config,
            project_name=project_name,
            acceptance_run_id=run_id,
            expected_services=expected_services,
            expect_runs_volume=expect_runs_volume,
            docker_runner=lambda command: runner(command, capture=True),
        )
    except acceptance_support.AcceptanceSupportError as exc:
        raise AcceptanceEnvironmentError("isolated Compose cleanup left residue") from exc


def _capture_cleanup_error(errors: list[BaseException], action: Callable[[], object]) -> bool:
    try:
        action()
    except BaseException as exc:
        errors.append(exc)
        return False
    return True


def cleanup_isolated_runtime(
    *,
    profile_name: str,
    expected_services: tuple[str, ...],
    compose_base: list[str],
    project_name: str,
    run_id: str,
    runtime: IsolatedRuntimeAuthority,
    runner: CleanupCommandRunner,
) -> None:
    errors: list[BaseException] = []
    cleanup_code = (
        "import shutil; from pathlib import Path; "
        "targets=(Path('/data'),Path('/governor-workspace'),Path('/claude-roots/governor')); "
        "[(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) "
        "for root in targets for p in tuple(root.iterdir())]"
    )
    volume_safe = profile_name != "agent-test"
    if profile_name == "agent-test":
        _capture_cleanup_error(
            errors,
            lambda: runner([*compose_base, "stop", "--timeout", _COMPOSE_STOP_TIMEOUT_SECONDS, "agent-test-worker"]),
        )
        _capture_cleanup_error(errors, lambda: runner([*compose_base, "rm", "--force", "--stop", "agent-test-worker"]))
        volume_safe = _capture_cleanup_error(errors, lambda: _cleanup_agent_test_volume(compose_base, project_name, runner))
    _capture_cleanup_error(
        errors,
        lambda: runner([*compose_base, "run", "--rm", "--no-deps", "--entrypoint", "/usr/local/bin/python", "claude-agent-api", "-c", cleanup_code]),
    )
    down = [*compose_base, "down", "--timeout", _COMPOSE_STOP_TIMEOUT_SECONDS, "--remove-orphans"]
    if volume_safe:
        down.insert(-1, "--volumes")
    _capture_cleanup_error(errors, lambda: runner(down))
    _capture_cleanup_error(
        errors,
        lambda: _cleanup_compose_residue(
            compose_base,
            project_name=project_name,
            run_id=run_id,
            expected_services=expected_services,
            expect_runs_volume=profile_name == "agent-test",
            runner=runner,
        ),
    )
    _capture_cleanup_error(errors, lambda: cleanup_candidate_runtime_root(runtime))
    if errors:
        raise AcceptanceEnvironmentError("隔离验收 cleanup 未完全收口") from errors[0]


def _require_current_parent(authority: IsolatedRuntimeAuthority) -> None:
    try:
        current_fd = os.open(authority.snapshot_parent, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise AcceptanceEnvironmentError("候选快照父目录 identity 已变化，已保留 residue") from exc
    try:
        current = os.fstat(current_fd)
        retained_snapshot_parent = os.fstat(authority.snapshot_parent_descriptor)
        retained = os.fstat(authority.parent_descriptor)
        snapshot_parent = (authority.snapshot_parent_device, authority.snapshot_parent_inode)
        candidate_root = (authority.parent_device, authority.parent_inode)
        if (
            (current.st_dev, current.st_ino) != snapshot_parent
            or (retained_snapshot_parent.st_dev, retained_snapshot_parent.st_ino) != snapshot_parent
            or (retained.st_dev, retained.st_ino) != candidate_root
        ):
            raise AcceptanceEnvironmentError("候选验收运行父目录 identity 已变化，已保留 residue")
        _require_linked_directory(current_fd, authority.path.parent.name, device=candidate_root[0], inode=candidate_root[1])
        _require_linked_directory(
            authority.snapshot_parent_descriptor,
            authority.path.parent.name,
            device=candidate_root[0],
            inode=candidate_root[1],
        )
    finally:
        os.close(current_fd)


def _require_entry(directory_fd: int, leaf: str, expected: os.stat_result) -> None:
    observed = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    if (observed.st_dev, observed.st_ino, stat.S_IFMT(observed.st_mode)) != (
        expected.st_dev,
        expected.st_ino,
        stat.S_IFMT(expected.st_mode),
    ):
        raise AcceptanceEnvironmentError("隔离验收子资源 identity 已变化，已保留 residue")


def _clear_directory(
    descriptor: int,
    *,
    expected_device: int,
    budget: _CleanupBudget,
    depth: int,
) -> None:
    if depth > _MAX_CLEANUP_DEPTH:
        raise AcceptanceEnvironmentError("隔离验收清理深度超限，已保留 residue")
    with os.scandir(descriptor) as entries:
        for entry in entries:
            budget.remaining -= 1
            if budget.remaining < 0:
                raise AcceptanceEnvironmentError("隔离验收清理条目超限，已保留 residue")
            _remove_runtime_entry(
                descriptor,
                entry.name,
                expected_device=expected_device,
                budget=budget,
                depth=depth,
            )
    os.fsync(descriptor)


def _remove_runtime_entry(
    descriptor: int,
    leaf: str,
    *,
    expected_device: int,
    budget: _CleanupBudget,
    depth: int,
) -> None:
    observed = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
    _require_entry(descriptor, leaf, observed)
    if stat.S_ISDIR(observed.st_mode):
        if observed.st_dev != expected_device:
            raise AcceptanceEnvironmentError("隔离验收子目录跨越文件系统，已保留 residue")
        child = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=descriptor)
        try:
            current = os.fstat(child)
            if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                raise AcceptanceEnvironmentError("隔离验收子目录 identity 已变化，已保留 residue")
            _clear_directory(
                child,
                expected_device=expected_device,
                budget=budget,
                depth=depth + 1,
            )
        finally:
            os.close(child)
        _require_entry(descriptor, leaf, observed)
        os.rmdir(leaf, dir_fd=descriptor)
    else:
        _require_entry(descriptor, leaf, observed)
        os.unlink(leaf, dir_fd=descriptor)


def _require_empty_root(authority: IsolatedRuntimeAuthority) -> None:
    authority.assert_current()
    with os.scandir(authority.root_descriptor) as entries:
        if next(entries, None) is not None:
            raise AcceptanceEnvironmentError("候选验收运行目录仍有 residue")


def cleanup_candidate_runtime_root(authority: IsolatedRuntimeAuthority) -> None:
    authority.cleanup()


def cleanup_stale_prepared(
    identity: receipt_authority.PreparedReceiptIdentity,
    fixed_docker_runner: acceptance_support.DockerRunner,
) -> None:
    """按回执冻结 identity 清理 stale prepared 的 Docker scope 与候选快照。"""
    profile = acceptance_contract.PROFILES.get(identity.profile)
    if profile is None:
        raise AcceptanceEnvironmentError("stale prepared profile authority 无效")
    if profile.isolated_runtime:
        project_name = f"agentgov-acceptance-{_runtime_token(identity.run_id)}"
        try:
            acceptance_support.cleanup_stale_acceptance_scope(
                profile=identity.profile,
                run_id=identity.run_id,
                project_name=project_name,
                expected_services=profile.expected_services,
                docker_runner=fixed_docker_runner,
            )
        except acceptance_support.AcceptanceSupportError as exc:
            raise AcceptanceEnvironmentError("stale prepared Docker scope 未完全清理") from exc
    try:
        acceptance_candidate.recover_and_cleanup_candidate_snapshot(identity.candidate_snapshot)
    except acceptance_candidate.CandidateSnapshotError as exc:
        raise AcceptanceEnvironmentError("stale prepared 候选快照未完全清理") from exc
