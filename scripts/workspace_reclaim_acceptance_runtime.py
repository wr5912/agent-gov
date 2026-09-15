"""Workspace reclaim 验收的 Runtime 容器、文件系统与真实信号边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeAlias, TypedDict, cast

from agentgov_agentscope_contract import version_workspace_id

from scripts.workspace_reclaim_acceptance_watchdog import process_is_stopped
from scripts.workspace_reclaim_acceptance_watchdog_control import (
    RuntimeWatchdog,
    WatchdogControllerError,
    arm_runtime_watchdog,
    disarm_runtime_watchdog,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
RUNTIME_WORKSPACES_DESTINATION: Final = "/runtime-workspaces"
RECLAIM_DIRECTORY: Final = ".agentgov-session-workspace-reclaim"
WORKSPACE_MARKER: Final = ".agentgov-runtime-workspace.json"
VENV_RELATIVE: Final = Path(".agentgov-runtime-state/.agentscope/.venv")
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
SAFE_COMPONENT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}")
FAILURE_CODE_PATTERN: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class AcceptanceFailure(RuntimeError):
    """Only a fixed non-sensitive failure code may cross the report boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code if FAILURE_CODE_PATTERN.fullmatch(code) else "UNEXPECTED_ACCEPTANCE_FAILURE")


class TreeUsageReport(TypedDict):
    directories: int
    files: int
    symlinks: int
    other_entries: int
    logical_bytes: int
    allocated_bytes: int


class ReclaimedPathsReport(TypedDict):
    target_absent: Literal[True]
    record_absent: Literal[True]
    tombstone_absent: Literal[True]
    temporary_records_absent: Literal[True]


class ArtifactReport(TypedDict):
    session_sha256: str
    workspace_sha256: str
    workspace_before: TreeUsageReport
    workspace_after: TreeUsageReport
    venv_before: TreeUsageReport
    venv_after: TreeUsageReport
    marker_sha256: str
    venv_config_sha256: str
    reclaimed_paths: ReclaimedPathsReport


class ArtifactIdentityReport(TypedDict):
    session_id_match: Literal[True]
    workspace_id_match: Literal[True]
    workspace_identity_match: Literal[True]
    marker_identity_match: Literal[True]
    venv_identity_match: Literal[True]
    venv_config_identity_match: Literal[True]
    harness_digest: str
    marker_sha256: str
    venv_config_sha256: str
    workspace_identity_sha256: str
    marker_identity_sha256: str
    venv_identity_sha256: str
    venv_config_identity_sha256: str


SidecarPhase = Literal["prepared", "quarantined", "contents_removed"]
SIDECAR_PHASES: Final[tuple[SidecarPhase, ...]] = ("prepared", "quarantined", "contents_removed")
SIDECAR_SNAPSHOT_ATTEMPTS: Final = len(SIDECAR_PHASES)


@dataclass(frozen=True)
class TreeUsage:
    directories: int = 0
    files: int = 0
    symlinks: int = 0
    other_entries: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0

    def report(self) -> TreeUsageReport:
        return TreeUsageReport(
            directories=self.directories,
            files=self.files,
            symlinks=self.symlinks,
            other_entries=self.other_entries,
            logical_bytes=self.logical_bytes,
            allocated_bytes=self.allocated_bytes,
        )


@dataclass(frozen=True)
class SessionArtifact:
    session_id: str
    workspace_id: str
    target: Path
    workspace_usage: TreeUsage
    venv_usage: TreeUsage
    marker_sha256: str
    venv_config_sha256: str
    harness_digest: str
    device: int
    inode: int
    marker_device: int
    marker_inode: int
    venv_device: int
    venv_inode: int
    venv_config_device: int
    venv_config_inode: int


@dataclass(frozen=True)
class WorkspaceReclaimLocator:
    session_id: str
    workspace_id: str
    target: Path


ReclaimTarget: TypeAlias = SessionArtifact | WorkspaceReclaimLocator


@dataclass(frozen=True)
class RuntimeMount:
    root: Path
    root_sha256: str
    container_id: str
    container_sha256: str
    image_sha256: str
    restart_count: int


@dataclass(frozen=True)
class CapturedRuntimeWindow:
    runtime_pid: int
    phase: SidecarPhase
    watchdog: RuntimeWatchdog


@dataclass(frozen=True)
class RuntimeRestartEvidence:
    restart_count: int
    runtime_pid: int


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _docker_json(container_id: str, template: str) -> object:
    docker = "/usr/bin/docker"
    if not Path(docker).is_file():
        raise AcceptanceFailure("RUNTIME_CONTAINER_INVALID")
    command = [docker, "--host", "unix:///var/run/docker.sock", "inspect", "--format", template, container_id]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env={key: os.environ[key] for key in ("HOME", "LANG", "PATH") if key in os.environ},
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise AcceptanceFailure("RUNTIME_CONTAINER_INVALID") from exc


def _expected_workspace_mount(values: Mapping[str, str]) -> Path:
    explicit = values.get("HOST_AGENTSCOPE_RUNTIME_WORKSPACES_MOUNT", "").strip()
    if explicit:
        return Path(explicit)
    root = values.get("HOST_RUNTIME_VOLUME_ROOT", "").strip()
    if not root:
        raise AcceptanceFailure("RUNTIME_MOUNT_INVALID")
    return Path(root) / "agentscope-runtime/workspaces"


def runtime_mount(container: Mapping[str, object], values: Mapping[str, str]) -> RuntimeMount:
    container_id = container.get("id")
    image = container.get("image")
    if not isinstance(container_id, str) or not SHA256_PATTERN.fullmatch(container_id) or not isinstance(image, str) or not image:
        raise AcceptanceFailure("RUNTIME_CONTAINER_INVALID")
    payload = _docker_json(
        container_id,
        '{"mounts":{{json .Mounts}},"restart_count":{{json .RestartCount}},'
        '"restart_policy":{{json .HostConfig.RestartPolicy.Name}},"running":{{json .State.Running}}}',
    )
    if (
        not isinstance(payload, dict)
        or payload.get("running") is not True
        or payload.get("restart_policy") != "unless-stopped"
        or type(payload.get("restart_count")) is not int
    ):
        raise AcceptanceFailure("RUNTIME_CONTAINER_INVALID")
    mounts = payload.get("mounts")
    selected = (
        [item for item in mounts if isinstance(item, dict) and item.get("Destination") == RUNTIME_WORKSPACES_DESTINATION] if isinstance(mounts, list) else []
    )
    if len(selected) != 1:
        raise AcceptanceFailure("RUNTIME_MOUNT_INVALID")
    source = _validated_mount_source(selected[0], values)
    return RuntimeMount(
        root=source,
        root_sha256=sha256_text(str(source)),
        container_id=container_id,
        container_sha256=sha256_text(container_id),
        image_sha256=sha256_text(image),
        restart_count=int(payload["restart_count"]),
    )


def _validated_mount_source(mount: Mapping[str, object], values: Mapping[str, str]) -> Path:
    raw_source = mount.get("Source")
    if mount.get("Type") != "bind" or mount.get("RW") is not True or not isinstance(raw_source, str) or not raw_source:
        raise AcceptanceFailure("RUNTIME_MOUNT_INVALID")
    source = Path(raw_source)
    expected = _expected_workspace_mount(values)
    try:
        source_info = source.lstat()
        expected_info = expected.lstat()
    except OSError as exc:
        raise AcceptanceFailure("RUNTIME_MOUNT_INVALID") from exc
    resolved = source.resolve()
    valid = (
        stat.S_ISDIR(source_info.st_mode)
        and not source.is_symlink()
        and stat.S_ISDIR(expected_info.st_mode)
        and not expected.is_symlink()
        and resolved == expected.resolve()
        and not resolved.is_relative_to(REPO_ROOT.resolve())
    )
    if not valid:
        raise AcceptanceFailure("RUNTIME_MOUNT_INVALID")
    return resolved


def safe_workspace_path(root: Path, workspace_id: str) -> Path:
    if SAFE_COMPONENT.fullmatch(workspace_id) is None:
        raise AcceptanceFailure("SESSION_WORKSPACE_INVALID")
    target = root / workspace_id
    if target.parent != root:
        raise AcceptanceFailure("SESSION_WORKSPACE_INVALID")
    return target


def tree_usage(root: Path) -> TreeUsage:
    if root.is_symlink() or not root.is_dir():
        raise AcceptanceFailure("REAL_VENV_NOT_MATERIALIZED")
    directories = files = symlinks = other_entries = logical_bytes = allocated_bytes = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise AcceptanceFailure("WORKSPACE_USAGE_UNSTABLE") from exc
        logical_bytes += metadata.st_size
        allocated_bytes += metadata.st_blocks * 512
        if stat.S_ISDIR(metadata.st_mode):
            directories += 1
            try:
                pending.extend(Path(entry.path) for entry in os.scandir(current))
            except OSError as exc:
                raise AcceptanceFailure("WORKSPACE_USAGE_UNSTABLE") from exc
        elif stat.S_ISREG(metadata.st_mode):
            files += 1
        elif stat.S_ISLNK(metadata.st_mode):
            symlinks += 1
        else:
            other_entries += 1
    return TreeUsage(directories, files, symlinks, other_entries, logical_bytes, allocated_bytes)


def artifact_report(artifact: SessionArtifact, reclaimed_paths: ReclaimedPathsReport) -> ArtifactReport:
    absent = TreeUsage().report()
    return ArtifactReport(
        session_sha256=sha256_text(artifact.session_id),
        workspace_sha256=sha256_text(artifact.workspace_id),
        workspace_before=artifact.workspace_usage.report(),
        workspace_after=absent,
        venv_before=artifact.venv_usage.report(),
        venv_after=absent,
        marker_sha256=artifact.marker_sha256,
        venv_config_sha256=artifact.venv_config_sha256,
        reclaimed_paths=reclaimed_paths,
    )


def _identity_sha256(device: int, inode: int) -> str:
    return sha256_text(f"{device}:{inode}")


def require_same_artifact_identity(before: SessionArtifact, after: SessionArtifact) -> ArtifactIdentityReport:
    """只核对对话不应改变的身份和具体文件内容，不比较可变目录用量。"""

    checks = (
        before.session_id == after.session_id,
        before.workspace_id == after.workspace_id,
        before.target == after.target and (before.device, before.inode) == (after.device, after.inode),
        (before.marker_device, before.marker_inode, before.marker_sha256) == (after.marker_device, after.marker_inode, after.marker_sha256),
        (before.venv_device, before.venv_inode) == (after.venv_device, after.venv_inode),
        (before.venv_config_device, before.venv_config_inode, before.venv_config_sha256)
        == (after.venv_config_device, after.venv_config_inode, after.venv_config_sha256),
        before.harness_digest == after.harness_digest,
    )
    if not all(checks):
        raise AcceptanceFailure("SURVIVOR_SESSION_DAMAGED")
    return ArtifactIdentityReport(
        session_id_match=True,
        workspace_id_match=True,
        workspace_identity_match=True,
        marker_identity_match=True,
        venv_identity_match=True,
        venv_config_identity_match=True,
        harness_digest=after.harness_digest,
        marker_sha256=after.marker_sha256,
        venv_config_sha256=after.venv_config_sha256,
        workspace_identity_sha256=_identity_sha256(after.device, after.inode),
        marker_identity_sha256=_identity_sha256(after.marker_device, after.marker_inode),
        venv_identity_sha256=_identity_sha256(after.venv_device, after.venv_inode),
        venv_config_identity_sha256=_identity_sha256(after.venv_config_device, after.venv_config_inode),
    )


def validate_artifact(root: Path, session_id: str, workspace_id: str, harness_digest: str) -> SessionArtifact:
    try:
        version_id = version_workspace_id(workspace_id)
    except ValueError as exc:
        raise AcceptanceFailure("SESSION_WORKSPACE_INVALID") from exc
    if not version_id.startswith("published-") or not version_id.endswith(f"--v-{harness_digest}"):
        raise AcceptanceFailure("SESSION_WORKSPACE_INVALID")
    target = safe_workspace_path(root, workspace_id)
    marker = target / WORKSPACE_MARKER
    venv = target / VENV_RELATIVE
    venv_config = venv / "pyvenv.cfg"
    try:
        marker_bytes = marker.read_bytes()
        marker_payload = json.loads(marker_bytes)
        marker_identity = marker.lstat()
    except (OSError, ValueError) as exc:
        raise AcceptanceFailure("SESSION_WORKSPACE_INVALID") from exc
    if marker_payload != {"workspace_id": workspace_id, "harness_digest": harness_digest} or marker.is_symlink() or not stat.S_ISREG(marker_identity.st_mode):
        raise AcceptanceFailure("SESSION_WORKSPACE_INVALID")
    if venv_config.is_symlink() or not venv_config.is_file():
        raise AcceptanceFailure("REAL_VENV_NOT_MATERIALIZED")
    first = (tree_usage(target), tree_usage(venv))
    time.sleep(0.05)
    second = (tree_usage(target), tree_usage(venv))
    if first != second or second[1].files < 1 or second[1].allocated_bytes <= 0:
        raise AcceptanceFailure("WORKSPACE_USAGE_UNSTABLE")
    identity = target.lstat()
    venv_identity = venv.lstat()
    venv_config_identity = venv_config.lstat()
    if not stat.S_ISDIR(identity.st_mode) or venv.is_symlink() or not stat.S_ISDIR(venv_identity.st_mode) or not stat.S_ISREG(venv_config_identity.st_mode):
        raise AcceptanceFailure("SESSION_WORKSPACE_INVALID")
    return SessionArtifact(
        session_id=session_id,
        workspace_id=workspace_id,
        target=target,
        workspace_usage=second[0],
        venv_usage=second[1],
        marker_sha256=hashlib.sha256(marker_bytes).hexdigest(),
        venv_config_sha256=_sha256_file(venv_config),
        harness_digest=harness_digest,
        device=identity.st_dev,
        inode=identity.st_ino,
        marker_device=marker_identity.st_dev,
        marker_inode=marker_identity.st_ino,
        venv_device=venv_identity.st_dev,
        venv_inode=venv_identity.st_ino,
        venv_config_device=venv_config_identity.st_dev,
        venv_config_inode=venv_config_identity.st_ino,
    )


def _reclaim_paths(mount: RuntimeMount, artifact: ReclaimTarget) -> tuple[Path, Path]:
    key = sha256_text(artifact.workspace_id)
    reclaim_root = mount.root / RECLAIM_DIRECTORY
    return reclaim_root / f"{key}.json", reclaim_root / key


def _temporary_records(record: Path) -> tuple[Path, ...]:
    key = record.name.removesuffix(".json")
    if not record.parent.is_dir():
        return ()
    return tuple(record.parent.glob(f".{key}.*.tmp"))


def _reclaimed_paths_report(mount: RuntimeMount, artifact: ReclaimTarget) -> ReclaimedPathsReport | None:
    record, tombstone = _reclaim_paths(mount, artifact)
    states = (
        not os.path.lexists(artifact.target),
        not os.path.lexists(record),
        not os.path.lexists(tombstone),
        not any(os.path.lexists(path) for path in _temporary_records(record)),
    )
    if not all(states):
        return None
    return ReclaimedPathsReport(
        target_absent=True,
        record_absent=True,
        tombstone_absent=True,
        temporary_records_absent=True,
    )


async def wait_reclaimed(
    mount: RuntimeMount,
    artifact: ReclaimTarget,
    timeout_seconds: float = 60.0,
) -> ReclaimedPathsReport:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        report = _reclaimed_paths_report(mount, artifact)
        if report is not None:
            return report
        await asyncio.sleep(0.05)
    raise AcceptanceFailure("WORKSPACE_NOT_RECLAIMED")


def _runtime_init_pid(container_id: str) -> int:
    value = _docker_json(container_id, "{{json .State.Pid}}")
    if type(value) is not int or value <= 1:
        raise AcceptanceFailure("RUNTIME_PROCESS_INVALID")
    return int(value)


def _process_children(pid: int) -> tuple[int, ...]:
    try:
        payload = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii").strip()
        return tuple(int(value) for value in payload.split()) if payload else ()
    except (OSError, ValueError) as exc:
        raise AcceptanceFailure("RUNTIME_PROCESS_INVALID") from exc


def _is_runtime_python(pid: int) -> bool:
    try:
        parts = [value for value in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if value]
    except OSError:
        return False
    return len(parts) >= 3 and Path(os.fsdecode(parts[0])).name.startswith("python") and parts[1] == b"-m" and parts[2] == b"agentscope_runtime"


def runtime_python_pid(container_id: str) -> int:
    pending = [_runtime_init_pid(container_id)]
    visited: set[int] = set()
    candidates: list[int] = []
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        if _is_runtime_python(pid):
            candidates.append(pid)
        pending.extend(_process_children(pid))
    if len(candidates) != 1:
        raise AcceptanceFailure("RUNTIME_PROCESS_INVALID")
    return candidates[0]


def _validated_bindings(value: object) -> frozenset[tuple[str, str]]:
    if not isinstance(value, list):
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    bindings: set[tuple[str, str]] = set()
    for raw_binding in value:
        if not isinstance(raw_binding, list) or len(raw_binding) != 2 or not all(isinstance(item, str) and item for item in raw_binding):
            raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
        bindings.add((raw_binding[0], raw_binding[1]))
    if len(bindings) != len(value):
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    return frozenset(bindings)


def _sidecar_payload(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID") from exc
    expected = {
        "schema_version",
        "phase",
        "workspace_id",
        "harness_digest",
        "device",
        "inode",
        "marker_sha256",
        "ignored_bindings",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    return cast(dict[str, object], payload)


def _sidecar_live_path(artifact: SessionArtifact, tombstone: Path, phase: SidecarPhase) -> Path:
    target_exists = os.path.lexists(artifact.target)
    tombstone_exists = os.path.lexists(tombstone)
    if target_exists == tombstone_exists or (phase != "prepared" and not tombstone_exists):
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    selected = artifact.target if target_exists else tombstone
    try:
        identity = selected.lstat()
    except OSError as exc:
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID") from exc
    if selected.is_symlink() or not stat.S_ISDIR(identity.st_mode):
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    if identity.st_dev != artifact.device or identity.st_ino != artifact.inode:
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    return selected


def _validate_sidecar_marker(path: Path, artifact: SessionArtifact, phase: SidecarPhase) -> None:
    marker = path / WORKSPACE_MARKER
    if phase == "contents_removed":
        try:
            if {entry.name for entry in path.iterdir()} - {WORKSPACE_MARKER}:
                raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
        except OSError as exc:
            raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID") from exc
    if not os.path.lexists(marker):
        try:
            if phase == "contents_removed" and not any(path.iterdir()):
                return
        except OSError as exc:
            raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID") from exc
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    if marker.is_symlink() or not marker.is_file():
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    try:
        marker_bytes = marker.read_bytes()
    except OSError as exc:
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID") from exc
    if hashlib.sha256(marker_bytes).hexdigest() != artifact.marker_sha256:
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")


def _validated_sidecar_identity(
    record: Path,
    tombstone: Path,
    artifact: SessionArtifact,
    expected_binding: tuple[str, str],
) -> SidecarPhase:
    payload = _sidecar_payload(record)
    schema_version = payload.get("schema_version")
    phase = payload.get("phase")
    device = payload.get("device")
    inode = payload.get("inode")
    valid = (
        type(schema_version) is int
        and schema_version == 1
        and isinstance(phase, str)
        and phase in {"prepared", "quarantined", "contents_removed"}
        and payload.get("workspace_id") == artifact.workspace_id
        and payload.get("harness_digest") == artifact.harness_digest
        and payload.get("marker_sha256") == artifact.marker_sha256
        and type(device) is int
        and int(device) == artifact.device
        and type(inode) is int
        and int(inode) == artifact.inode
    )
    key = sha256_text(artifact.workspace_id)
    reclaim_root = artifact.target.parent / RECLAIM_DIRECTORY
    if not valid or record != reclaim_root / f"{key}.json" or tombstone != reclaim_root / key:
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    if _validated_bindings(payload.get("ignored_bindings")) != frozenset({expected_binding}):
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")
    return cast(SidecarPhase, phase)


def validate_reclaim_sidecar(
    record: Path,
    tombstone: Path,
    artifact: SessionArtifact,
    expected_binding: tuple[str, str],
) -> SidecarPhase:
    typed_phase = _validated_sidecar_identity(record, tombstone, artifact, expected_binding)
    _validate_reclaim_sidecar_live(tombstone, artifact, typed_phase)
    return typed_phase


def _validate_reclaim_sidecar_live(
    tombstone: Path,
    artifact: SessionArtifact,
    phase: SidecarPhase,
) -> None:
    live_path = _sidecar_live_path(artifact, tombstone, phase)
    _validate_sidecar_marker(live_path, artifact, phase)


def _require_monotonic_sidecar_phase(previous: SidecarPhase | None, current: SidecarPhase) -> None:
    if previous is not None and SIDECAR_PHASES.index(current) < SIDECAR_PHASES.index(previous):
        raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")


def _validated_sidecar_or_completed(
    record: Path,
    tombstone: Path,
    artifact: SessionArtifact,
    mount: RuntimeMount,
    expected_binding: tuple[str, str],
) -> SidecarPhase | None:
    previous_phase: SidecarPhase | None = None
    for attempt in range(SIDECAR_SNAPSHOT_ATTEMPTS):
        try:
            phase = _validated_sidecar_identity(record, tombstone, artifact, expected_binding)
        except AcceptanceFailure:
            if _reclaimed_paths_report(mount, artifact) is not None:
                return None
            raise
        _require_monotonic_sidecar_phase(previous_phase, phase)
        previous_phase = phase
        try:
            _validate_reclaim_sidecar_live(tombstone, artifact, phase)
        except AcceptanceFailure:
            if _reclaimed_paths_report(mount, artifact) is not None:
                return None
            if not os.path.lexists(artifact.target) and not os.path.lexists(tombstone) and phase == "contents_removed":
                return None
            if attempt + 1 < SIDECAR_SNAPSHOT_ATTEMPTS:
                continue
            raise
        return phase
    raise AcceptanceFailure("RECLAIM_SIDECAR_INVALID")


async def capture_tombstone(
    artifact: SessionArtifact,
    mount: RuntimeMount,
    runtime_pid: int,
    restart_count: int,
    delete_task: asyncio.Task[int],
    expected_binding: tuple[str, str],
) -> CapturedRuntimeWindow | None:
    record, tombstone = _reclaim_paths(mount, artifact)
    deadline = asyncio.get_running_loop().time() + 30.0
    while asyncio.get_running_loop().time() < deadline:
        if os.path.lexists(record):
            captured = await _stop_runtime_if_pending(
                artifact,
                mount,
                runtime_pid,
                restart_count,
                record,
                tombstone,
                expected_binding,
            )
            if captured is not None:
                return captured
        if delete_task.done() and _reclaimed_paths_report(mount, artifact) is not None:
            return None
        await asyncio.sleep(0.001)
    return None


async def _stop_runtime_if_pending(
    artifact: SessionArtifact,
    mount: RuntimeMount,
    runtime_pid: int,
    restart_count: int,
    record: Path,
    tombstone: Path,
    expected_binding: tuple[str, str],
) -> CapturedRuntimeWindow | None:
    stopped = False
    captured = False
    watchdog: RuntimeWatchdog | None = None
    try:
        phase = _validated_sidecar_or_completed(record, tombstone, artifact, mount, expected_binding)
        if phase is None:
            return None
        if runtime_python_pid(mount.container_id) != runtime_pid:
            raise AcceptanceFailure("RUNTIME_PROCESS_INVALID")
        try:
            watchdog = await asyncio.to_thread(arm_runtime_watchdog, runtime_pid)
        except WatchdogControllerError as exc:
            raise AcceptanceFailure("RUNTIME_WATCHDOG_FAILED") from exc
        phase = _validated_sidecar_or_completed(record, tombstone, artifact, mount, expected_binding)
        if phase is None:
            return None
        os.kill(runtime_pid, signal.SIGSTOP)
        stopped = True
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not process_is_stopped(runtime_pid):
            await asyncio.sleep(0.005)
        if (
            process_is_stopped(runtime_pid)
            and runtime_python_pid(mount.container_id) == runtime_pid
            and runtime_restart_count(mount.container_id) == restart_count
        ):
            phase = _validated_sidecar_or_completed(record, tombstone, artifact, mount, expected_binding)
            if phase is None:
                return None
            captured = True
            return CapturedRuntimeWindow(runtime_pid, phase, watchdog)
    except OSError as exc:
        raise AcceptanceFailure("RUNTIME_SIGNAL_FAILED") from exc
    finally:
        if stopped and not captured:
            resume_runtime(runtime_pid)
        if watchdog is not None and not captured:
            await asyncio.to_thread(disarm_runtime_watchdog, watchdog)
    return None


def resume_runtime(runtime_pid: int | None) -> None:
    if runtime_pid is not None and _is_runtime_python(runtime_pid) and process_is_stopped(runtime_pid):
        with suppress(OSError):
            os.kill(runtime_pid, signal.SIGCONT)


def kill_runtime(runtime_pid: int) -> None:
    if not _is_runtime_python(runtime_pid) or not process_is_stopped(runtime_pid):
        raise AcceptanceFailure("RUNTIME_PROCESS_INVALID")
    try:
        os.kill(runtime_pid, signal.SIGKILL)
    except OSError as exc:
        raise AcceptanceFailure("RUNTIME_SIGNAL_FAILED") from exc


def runtime_restart_count(container_id: str) -> int:
    value = _docker_json(container_id, "{{json .RestartCount}}")
    if type(value) is not int or value < 0:
        raise AcceptanceFailure("RUNTIME_CONTAINER_INVALID")
    return int(value)


async def wait_runtime_restart(
    mount: RuntimeMount,
    previous_pid: int,
    previous_restart_count: int,
) -> RuntimeRestartEvidence:
    deadline = asyncio.get_running_loop().time() + 90.0
    while asyncio.get_running_loop().time() < deadline:
        try:
            payload = _docker_json(
                mount.container_id,
                '{"restart_count":{{json .RestartCount}},"running":{{json .State.Running}},"health":{{json .State.Health.Status}}}',
            )
            restarted_pid = runtime_python_pid(mount.container_id)
            if _is_restarted(payload, previous_restart_count) and restarted_pid != previous_pid:
                return RuntimeRestartEvidence(int(payload["restart_count"]), restarted_pid)  # type: ignore[index]
        except AcceptanceFailure:
            pass
        await asyncio.sleep(0.25)
    raise AcceptanceFailure("RUNTIME_RESTART_FAILED")


def _is_restarted(payload: object, previous_count: int) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("running") is True
        and payload.get("health") == "healthy"
        and type(payload.get("restart_count")) is int
        and int(payload["restart_count"]) > previous_count
    )
