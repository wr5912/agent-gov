from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from pydantic import TypeAdapter, ValidationError
from pydantic.types import JsonValue

from agentgov_testkit.pytest_plugin import WORKSPACE_REPORT_LOG_PREFIX
from app.runtime.json_types import JsonObject

from .docker_engine import (
    DockerEngineClient,
    DockerEngineError,
    DockerLogs,
    validate_docker_volume_name,
    validated_local_volume_mountpoint,
)
from .docker_volume_contracts import SANDBOX_KIND_LABEL, SANDBOX_SCOPE_LABEL
from .execution_contracts import (
    FIXED_PYTEST_COMMAND,
    SANDBOX_LOG_MAX_BYTES,
    SANDBOX_LOG_MAX_FILES,
    SANDBOX_MEMORY_BYTES,
    SANDBOX_NANO_CPUS,
    SANDBOX_PIDS_LIMIT,
    SANDBOX_SHM_SIZE_BYTES,
    SANDBOX_TMPFS_SIZE_BYTES,
    SANDBOX_USER,
    SANDBOX_WORKSPACE_MOUNT_TYPE,
    SANDBOX_WORKSPACE_SOURCE_SCOPE,
    AgentOwnedPytestReport,
    AgentTestIsolationReceipt,
    AgentTestSandboxMountReceipt,
    sandbox_environment,
)

SANDBOX_RUN_LABEL: Final = "io.agentgov.agent-test.run-id"
SANDBOX_WORKSPACE: Final = "/workspace"
SANDBOX_OUTPUT: Final = "/output"
SANDBOX_TMPFS: Final = {
    "/output": f"rw,noexec,nosuid,nodev,size={SANDBOX_TMPFS_SIZE_BYTES},mode=1777",
    "/tmp": f"rw,noexec,nosuid,nodev,size={SANDBOX_TMPFS_SIZE_BYTES},mode=1777",
}
MAX_CAPTURED_OUTPUT_BYTES: Final = 256_000
MAX_REPORT_LOG_BYTES: Final = MAX_CAPTURED_OUTPUT_BYTES + len(WORKSPACE_REPORT_LOG_PREFIX.encode("utf-8")) + 1024

_RUN_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_COMMIT_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
_REPORT_ADAPTER = TypeAdapter(JsonObject)


class DockerEngine(Protocol):
    def resolve_image_id(self, image_ref: str) -> str: ...

    def create_container(self, *, name: str, config: JsonObject) -> str: ...

    def inspect_container(self, container_id: str) -> JsonObject: ...

    def inspect_volume(self, volume_name: str) -> JsonObject: ...

    def start_container(self, container_id: str) -> None: ...

    def kill_container(self, container_id: str) -> None: ...

    def remove_container(self, container_id: str) -> None: ...

    def container_ids_with_label(self, label: str) -> tuple[str, ...]: ...

    def read_container_logs(
        self,
        container_id: str,
        *,
        max_bytes_per_stream: int,
        tail_lines: int | None = None,
        include_stderr: bool = True,
    ) -> DockerLogs: ...


class SandboxExecutorError(RuntimeError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SandboxRunSpec:
    run_id: str
    scope_id: str
    agent_id: str
    commit_sha: str
    workspace_path: Path
    runs_volume_name: str
    image_ref: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        if _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise ValueError("run_id contains unsupported characters")
        if _RUN_ID_PATTERN.fullmatch(self.scope_id) is None:
            raise ValueError("scope_id contains unsupported characters")
        if not self.agent_id.strip() or len(self.agent_id) > 128:
            raise ValueError("agent_id must be non-empty and at most 128 characters")
        if _COMMIT_PATTERN.fullmatch(self.commit_sha) is None:
            raise ValueError("commit_sha must be a full lowercase 40-character SHA")
        validate_docker_volume_name(self.runs_volume_name)
        if not self.image_ref.strip() or len(self.image_ref) > 512 or any(ord(char) < 32 for char in self.image_ref):
            raise ValueError("image_ref is invalid")
        if not 1 <= self.timeout_seconds <= 86_400:
            raise ValueError("timeout_seconds must be between 1 and 86400")


@dataclass(frozen=True, slots=True)
class _IsolationAudit:
    receipt: AgentTestIsolationReceipt | None
    failed_checks: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.receipt is not None and not self.failed_checks


@dataclass(frozen=True, slots=True)
class SandboxCleanupResult:
    removed: bool
    labels_empty: bool
    error_message: str | None = None

    @property
    def complete(self) -> bool:
        return self.removed and self.labels_empty and self.error_message is None


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    image_id: str | None
    container_id: str | None
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    report: JsonObject | None
    isolation: AgentTestIsolationReceipt | None
    isolation_failed_checks: tuple[str, ...]
    cleanup: SandboxCleanupResult
    timed_out: bool
    cancelled: bool
    error_code: str | None
    error_message: str | None


@dataclass(slots=True)
class _ExecutionState:
    started: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    report: JsonObject | None = None
    timed_out: bool = False
    cancelled: bool = False
    error_code: str | None = None
    error_message: str | None = None


class ContainerSandboxExecutor:
    def __init__(
        self,
        *,
        engine: DockerEngine | None = None,
        poll_interval_seconds: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._engine = engine or DockerEngineClient()
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._sleep = sleep

    def execute(
        self,
        spec: SandboxRunSpec,
        *,
        cancel_requested: Callable[[], bool],
        on_container_created: Callable[[str], None],
    ) -> SandboxExecutionResult:
        workspace_subpath = _validated_run_workspace(spec)
        started_at = self._clock()
        state = _ExecutionState()
        isolation = _IsolationAudit(receipt=None, failed_checks=("inspect",))
        image_id: str | None = None
        container_id: str | None = None
        try:
            image_id = self._engine.resolve_image_id(spec.image_ref)
            config = _container_config(spec=spec, image_id=image_id, workspace_subpath=workspace_subpath)
            container_id = self._engine.create_container(name=f"agentgov-test-{spec.run_id}", config=config)
            on_container_created(container_id)
            isolation = _inspect_isolation(
                self._engine.inspect_container(container_id),
                volume_inspect=self._engine.inspect_volume(spec.runs_volume_name),
                image_id=image_id,
                runs_volume_name=spec.runs_volume_name,
                workspace_subpath=workspace_subpath,
                expected_env=sandbox_environment(),
                expected_run_id=spec.run_id,
                expected_scope_id=spec.scope_id,
            )
            if not isolation.verified:
                failed = ",".join(isolation.failed_checks)
                raise SandboxExecutorError(code="SANDBOX_INSPECT_MISMATCH", message=f"sandbox inspect rejected checks: {failed}")
            state.cancelled = bool(cancel_requested())
            if not state.cancelled:
                self._engine.start_container(container_id)
                state.started = True
                self._poll_until_terminal(container_id, spec=spec, state=state, started_at=started_at, cancel_requested=cancel_requested)
                self._ingest_result(container_id, state=state)
        except (DockerEngineError, SandboxExecutorError) as exc:
            state.error_code = exc.code
            state.error_message = str(exc)
        except Exception as exc:  # fail closed while preserving cleanup evidence
            state.error_code = "SANDBOX_EXECUTION_ERROR"
            state.error_message = f"{exc.__class__.__name__}: sandbox execution failed"
        cleanup = self._cleanup_container(container_id, run_id=spec.run_id)
        if not cleanup.complete:
            state.error_code = "SANDBOX_CLEANUP_FAILED"
            state.error_message = cleanup.error_message or "sandbox cleanup could not be proven"
        return SandboxExecutionResult(
            image_id=image_id,
            container_id=container_id,
            exit_code=state.exit_code,
            duration_seconds=max(0.0, self._clock() - started_at),
            stdout=state.stdout,
            stderr=state.stderr,
            report=state.report,
            isolation=isolation.receipt,
            isolation_failed_checks=isolation.failed_checks,
            cleanup=cleanup,
            timed_out=state.timed_out,
            cancelled=state.cancelled,
            error_code=state.error_code,
            error_message=state.error_message,
        )

    def _poll_until_terminal(
        self,
        container_id: str,
        *,
        spec: SandboxRunSpec,
        state: _ExecutionState,
        started_at: float,
        cancel_requested: Callable[[], bool],
    ) -> None:
        while True:
            inspect = self._engine.inspect_container(container_id)
            running, exit_code = _container_state(inspect)
            if not running:
                state.exit_code = exit_code
                return
            if cancel_requested():
                state.cancelled = True
                self._engine.kill_container(container_id)
                state.exit_code = _wait_after_kill(self._engine, container_id)
                return
            if self._clock() - started_at >= spec.timeout_seconds:
                state.timed_out = True
                self._engine.kill_container(container_id)
                state.exit_code = _wait_after_kill(self._engine, container_id)
                return
            self._sleep(self._poll_interval_seconds)

    def _ingest_result(self, container_id: str, *, state: _ExecutionState) -> None:
        logs = self._engine.read_container_logs(container_id, max_bytes_per_stream=MAX_CAPTURED_OUTPUT_BYTES)
        state.stdout = _strip_report_log_records(logs.stdout)
        state.stderr = logs.stderr
        try:
            report_log = self._engine.read_container_logs(
                container_id,
                max_bytes_per_stream=MAX_REPORT_LOG_BYTES,
                tail_lines=1,
                include_stderr=False,
            )
            state.report = _read_report_log(report_log.stdout)
        except (DockerEngineError, SandboxExecutorError):
            if not state.timed_out and not state.cancelled:
                state.error_code = "SANDBOX_REPORT_INVALID"
                state.error_message = "sandbox report is missing, oversized, or unsafe"

    def _cleanup_container(self, container_id: str | None, *, run_id: str) -> SandboxCleanupResult:
        removed = True
        labels_empty = False
        errors: list[str] = []
        try:
            labeled = set(self._engine.container_ids_with_label(f"{SANDBOX_RUN_LABEL}={run_id}"))
            if container_id is not None:
                labeled.add(container_id)
            for candidate in labeled:
                try:
                    self._engine.remove_container(candidate)
                except Exception as exc:
                    removed = False
                    errors.append(f"remove:{exc.__class__.__name__}")
        except Exception as exc:
            removed = False
            errors.append(f"label-list:{exc.__class__.__name__}")
        try:
            labels_empty = not self._engine.container_ids_with_label(f"{SANDBOX_RUN_LABEL}={run_id}")
            if not labels_empty:
                errors.append("label-residual")
        except Exception as exc:
            errors.append(f"label-audit:{exc.__class__.__name__}")
        return SandboxCleanupResult(removed=removed, labels_empty=labels_empty, error_message=",".join(errors) or None)


def _validated_run_workspace(spec: SandboxRunSpec) -> str:
    workspace = _validated_directory(spec.workspace_path, field="workspace_path")
    if workspace.name != "workspace" or workspace.parent.name != spec.run_id:
        raise SandboxExecutorError(code="SANDBOX_PATH_MAPPING_INVALID", message="worker workspace must belong to the selected run directory")
    return f"{spec.run_id}/workspace"


def _validated_directory(path: Path, *, field: str) -> Path:
    if not path.is_absolute():
        raise SandboxExecutorError(code="SANDBOX_HOST_PATH_INVALID", message=f"{field} must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SandboxExecutorError(code="SANDBOX_HOST_PATH_INVALID", message=f"{field} is unavailable") from exc
    if resolved != path or not resolved.is_dir():
        raise SandboxExecutorError(code="SANDBOX_HOST_PATH_INVALID", message=f"{field} must be a real directory without symlinks")
    return resolved


def _container_config(*, spec: SandboxRunSpec, image_id: str, workspace_subpath: str) -> JsonObject:
    environment = sandbox_environment()
    raw: object = {
        "Image": image_id,
        "Cmd": list(FIXED_PYTEST_COMMAND),
        "Entrypoint": [],
        "WorkingDir": SANDBOX_WORKSPACE,
        "User": SANDBOX_USER,
        "Env": [f"{key}={value}" for key, value in sorted(environment.items())],
        "Labels": {
            SANDBOX_KIND_LABEL: "true",
            SANDBOX_RUN_LABEL: spec.run_id,
            SANDBOX_SCOPE_LABEL: spec.scope_id,
        },
        "NetworkDisabled": True,
        "Tty": False,
        "OpenStdin": False,
        "HostConfig": {
            "AutoRemove": False,
            "CapAdd": [],
            "CapDrop": ["ALL"],
            "Devices": [],
            "DeviceCgroupRules": [],
            "DeviceRequests": [],
            "IpcMode": "private",
            "LogConfig": {
                "Type": "local",
                "Config": {
                    "compress": "false",
                    "max-file": str(SANDBOX_LOG_MAX_FILES),
                    "max-size": f"{SANDBOX_LOG_MAX_BYTES // (1024 * 1024)}m",
                },
            },
            "Memory": SANDBOX_MEMORY_BYTES,
            "MemorySwap": SANDBOX_MEMORY_BYTES,
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": spec.runs_volume_name,
                    "Target": SANDBOX_WORKSPACE,
                    "ReadOnly": True,
                    "VolumeOptions": {
                        "NoCopy": True,
                        "Subpath": workspace_subpath,
                    },
                }
            ],
            "NanoCpus": SANDBOX_NANO_CPUS,
            "NetworkMode": "none",
            "PidsLimit": SANDBOX_PIDS_LIMIT,
            "PidMode": "",
            "Privileged": False,
            "PublishAllPorts": False,
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "SecurityOpt": ["no-new-privileges:true"],
            "ShmSize": SANDBOX_SHM_SIZE_BYTES,
            "Tmpfs": dict(SANDBOX_TMPFS),
            "UTSMode": "",
        },
    }
    return _REPORT_ADAPTER.validate_python(raw)


def _inspect_isolation(
    inspect: JsonObject,
    *,
    volume_inspect: JsonObject,
    image_id: str,
    runs_volume_name: str,
    workspace_subpath: str,
    expected_env: Mapping[str, str],
    expected_run_id: str,
    expected_scope_id: str,
) -> _IsolationAudit:
    config = _object(inspect.get("Config"))
    host = _object(inspect.get("HostConfig"))
    try:
        volume_mountpoint = validated_local_volume_mountpoint(volume_inspect, expected_name=runs_volume_name)
    except ValueError:
        volume_mountpoint = None
    failed = _failed_isolation_checks(
        inspect,
        config=config,
        host=host,
        image_id=image_id,
        runs_volume_name=runs_volume_name,
        volume_mountpoint=volume_mountpoint,
        workspace_subpath=workspace_subpath,
        expected_env=expected_env,
        expected_run_id=expected_run_id,
        expected_scope_id=expected_scope_id,
    )
    if failed:
        return _IsolationAudit(receipt=None, failed_checks=failed)
    return _IsolationAudit(receipt=_verified_isolation_receipt(), failed_checks=())


def _failed_isolation_checks(
    inspect: JsonObject,
    *,
    config: Mapping[str, JsonValue],
    host: Mapping[str, JsonValue],
    image_id: str,
    runs_volume_name: str,
    volume_mountpoint: Path | None,
    workspace_subpath: str,
    expected_env: Mapping[str, str],
    expected_run_id: str,
    expected_scope_id: str,
) -> tuple[str, ...]:
    env = _environment(config.get("Env"))
    cap_drop = {item.upper() for item in _strings(host.get("CapDrop"))}
    security_options = set(_strings(host.get("SecurityOpt")))
    devices_empty = all(_empty(host.get(key)) for key in ("Devices", "DeviceCgroupRules", "DeviceRequests"))
    resources = (
        host.get("PidsLimit") == SANDBOX_PIDS_LIMIT
        and host.get("Memory") == SANDBOX_MEMORY_BYTES
        and host.get("MemorySwap") == SANDBOX_MEMORY_BYTES
        and host.get("NanoCpus") == SANDBOX_NANO_CPUS
    )
    restart_policy = _object(host.get("RestartPolicy"))
    log_config = _object(host.get("LogConfig"))
    log_options = _object(log_config.get("Config"))
    lifecycle = (
        host.get("AutoRemove") is False
        and host.get("PublishAllPorts") is False
        and restart_policy.get("Name") == "no"
        and restart_policy.get("MaximumRetryCount") == 0
    )
    checks = {
        "image": config.get("Image") == image_id,
        "command": (
            tuple(_strings(config.get("Cmd"))) == FIXED_PYTEST_COMMAND and config.get("WorkingDir") == SANDBOX_WORKSPACE and _empty(config.get("Entrypoint"))
        ),
        "environment": _safe_environment(env, expected=expected_env),
        "labels": _sandbox_labels_match(
            config.get("Labels"),
            expected_run_id=expected_run_id,
            expected_scope_id=expected_scope_id,
        ),
        "volume": volume_mountpoint is not None,
        "user": config.get("User") == SANDBOX_USER,
        "network": config.get("NetworkDisabled") is True and host.get("NetworkMode") == "none" and _empty(host.get("PortBindings")),
        "namespaces": host.get("PidMode") in (None, "") and host.get("UTSMode") in (None, "") and host.get("IpcMode") == "private",
        "read_only_rootfs": host.get("ReadonlyRootfs") is True,
        "capabilities": cap_drop == {"ALL"} and _empty(host.get("CapAdd")),
        "security_options": security_options in ({"no-new-privileges"}, {"no-new-privileges:true"}),
        "mounts": _mounts_match(
            inspect,
            host=host,
            runs_volume_name=runs_volume_name,
            volume_mountpoint=volume_mountpoint,
            workspace_subpath=workspace_subpath,
        ),
        "devices": devices_empty,
        "privileged": host.get("Privileged") is False,
        "resource_limits": resources,
        "tmpfs": _object(host.get("Tmpfs")) == SANDBOX_TMPFS,
        "lifecycle": lifecycle,
        "shm": host.get("ShmSize") == SANDBOX_SHM_SIZE_BYTES,
        "logs": (
            log_config.get("Type") == "local"
            and log_options.get("compress") == "false"
            and log_options.get("max-size") == f"{SANDBOX_LOG_MAX_BYTES // (1024 * 1024)}m"
            and log_options.get("max-file") == str(SANDBOX_LOG_MAX_FILES)
        ),
    }
    return tuple(name for name, passed in checks.items() if not passed)


def _verified_isolation_receipt() -> AgentTestIsolationReceipt:
    return AgentTestIsolationReceipt(
        user=SANDBOX_USER,
        network_mode="none",
        network_disabled=True,
        pid_mode="private",
        ipc_mode="private",
        uts_mode="private",
        readonly_rootfs=True,
        cap_drop=("ALL",),
        security_opt=("no-new-privileges",),
        privileged=False,
        devices=(),
        mounts=(
            AgentTestSandboxMountReceipt(
                target="/workspace",
                read_only=True,
                mount_type=SANDBOX_WORKSPACE_MOUNT_TYPE,
                source_scope=SANDBOX_WORKSPACE_SOURCE_SCOPE,
            ),
        ),
        pids_limit=SANDBOX_PIDS_LIMIT,
        memory_bytes=SANDBOX_MEMORY_BYTES,
        memory_swap_bytes=SANDBOX_MEMORY_BYTES,
        nano_cpus=SANDBOX_NANO_CPUS,
        tmpfs_targets=("/output", "/tmp"),
        tmpfs_size_bytes=SANDBOX_TMPFS_SIZE_BYTES,
        tmpfs_noexec=True,
        tmpfs_nosuid=True,
        tmpfs_nodev=True,
        shm_size_bytes=SANDBOX_SHM_SIZE_BYTES,
        ports_published=False,
        auto_remove=False,
        restart_policy="no",
        log_driver="local",
        log_max_bytes=SANDBOX_LOG_MAX_BYTES,
        log_max_files=SANDBOX_LOG_MAX_FILES,
        log_compression=False,
        docker_socket_mounted=False,
    )


def _mounts_match(
    inspect: JsonObject,
    *,
    host: Mapping[str, JsonValue],
    runs_volume_name: str,
    volume_mountpoint: Path | None,
    workspace_subpath: str,
) -> bool:
    if volume_mountpoint is None:
        return False
    if not _empty(host.get("Binds")) or not _empty(host.get("VolumesFrom")):
        return False
    configured_mounts = host.get("Mounts")
    actual_mounts = inspect.get("Mounts")
    if not isinstance(configured_mounts, list) or len(configured_mounts) != 1:
        return False
    if not isinstance(actual_mounts, list) or len(actual_mounts) != 1:
        return False
    configured = _object(configured_mounts[0])
    volume_options = _object(configured.get("VolumeOptions"))
    configured_matches = (
        configured.get("Type") == "volume"
        and configured.get("Source") == runs_volume_name
        and configured.get("Target") == SANDBOX_WORKSPACE
        and configured.get("ReadOnly") is True
        and volume_options == {"NoCopy": True, "Subpath": workspace_subpath}
        and _empty(configured.get("BindOptions"))
    )
    actual = _object(actual_mounts[0])
    actual_matches = (
        actual.get("Type") == "volume"
        and actual.get("Name") == runs_volume_name
        and actual.get("Source") == str(volume_mountpoint)
        and actual.get("Destination") == SANDBOX_WORKSPACE
        and actual.get("Driver") == "local"
        and actual.get("RW") is False
    )
    return configured_matches and actual_matches


def _safe_environment(actual: Mapping[str, str], *, expected: Mapping[str, str]) -> bool:
    return actual == expected


def _sandbox_labels_match(value: JsonValue | None, *, expected_run_id: str, expected_scope_id: str) -> bool:
    labels = _object(value)
    return (
        labels.get(SANDBOX_KIND_LABEL) == "true" and labels.get(SANDBOX_RUN_LABEL) == expected_run_id and labels.get(SANDBOX_SCOPE_LABEL) == expected_scope_id
    )


def _environment(value: JsonValue | None) -> Mapping[str, str]:
    result: dict[str, str] = {}
    for item in _strings(value):
        key, separator, raw_value = item.partition("=")
        if not separator or not key or key in result:
            return {}
        result[key] = raw_value
    return result


def _container_state(inspect: JsonObject) -> tuple[bool, int | None]:
    state = _object(inspect.get("State"))
    running = state.get("Running")
    exit_code = state.get("ExitCode")
    if not isinstance(running, bool) or isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise SandboxExecutorError(code="SANDBOX_STATE_INVALID", message="Docker inspect returned an invalid container state")
    return running, None if running else exit_code


def _wait_after_kill(engine: DockerEngine, container_id: str) -> int:
    for _ in range(50):
        running, exit_code = _container_state(engine.inspect_container(container_id))
        if not running and exit_code is not None:
            return exit_code
        time.sleep(0.1)
    raise SandboxExecutorError(code="SANDBOX_KILL_TIMEOUT", message="sandbox did not stop after kill")


def _read_report(payload: bytes) -> JsonObject:
    try:
        if len(payload) > MAX_CAPTURED_OUTPUT_BYTES:
            raise SandboxExecutorError(code="SANDBOX_REPORT_INVALID", message="sandbox report exceeded the output limit")
        parsed = AgentOwnedPytestReport.model_validate(json.loads(payload.decode("utf-8")))
        return _REPORT_ADAPTER.validate_python(parsed.model_dump(mode="json"))
    except (UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise SandboxExecutorError(code="SANDBOX_REPORT_INVALID", message="sandbox report is not a valid JSON object") from exc


def _read_report_log(stdout: str) -> JsonObject:
    lines = tuple(line for line in stdout.splitlines() if line.startswith(WORKSPACE_REPORT_LOG_PREFIX))
    if len(lines) != 1:
        raise SandboxExecutorError(code="SANDBOX_REPORT_INVALID", message="sandbox report log envelope is missing or ambiguous")
    payload = lines[0][len(WORKSPACE_REPORT_LOG_PREFIX) :].encode("utf-8")
    return _read_report(payload)


def _strip_report_log_records(stdout: str) -> str:
    return "".join(line for line in stdout.splitlines(keepends=True) if not line.startswith(WORKSPACE_REPORT_LOG_PREFIX))


def _object(value: JsonValue | None) -> Mapping[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _strings(value: JsonValue | None) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _empty(value: JsonValue | None) -> bool:
    return value is None or value == "" or value == [] or value == {}
