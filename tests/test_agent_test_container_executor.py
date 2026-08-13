from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from app.agent_testing.container_executor import (
    MAX_CAPTURED_OUTPUT_BYTES,
    SANDBOX_KIND_LABEL,
    SANDBOX_RUN_LABEL,
    SANDBOX_SCOPE_LABEL,
    ContainerSandboxExecutor,
    SandboxExecutorError,
    SandboxRunSpec,
)
from app.agent_testing.docker_engine import DockerEngineClient, DockerEngineError, DockerEngineProtocolError, DockerLogs
from app.agent_testing.execution_contracts import (
    FIXED_PYTEST_COMMAND,
    FIXED_SANDBOX_ENV,
    SANDBOX_MEMORY_BYTES,
    SANDBOX_NANO_CPUS,
    SANDBOX_PIDS_LIMIT,
    SANDBOX_USER,
)
from app.runtime.json_types import JsonObject
from pydantic.types import JsonValue

from agentgov_testkit.pytest_plugin import WORKSPACE_REPORT_LOG_PREFIX

IMAGE_ID = f"sha256:{'a' * 64}"
CONTAINER_ID = "b" * 64
RUNS_VOLUME_NAME = "agent-gov_agent-test-runs"

InspectMutationTarget = Literal[
    "config",
    "host",
    "configured-mount",
    "volume-options",
    "actual-mount",
    "labels",
]


@dataclass(frozen=True)
class _InspectMutation:
    target: InspectMutationTarget
    key: str
    value: JsonValue


_INSPECT_MUTATIONS = {
    "entrypoint": _InspectMutation("config", "Entrypoint", ["/bin/sh", "-c"]),
    "device": _InspectMutation(
        "host",
        "Devices",
        [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda", "CgroupPermissions": "rwm"}],
    ),
    "mount-source": _InspectMutation("configured-mount", "Source", "attacker-volume"),
    "mount-subpath": _InspectMutation("volume-options", "Subpath", "other-run/workspace"),
    "mount-type": _InspectMutation("configured-mount", "Type", "bind"),
    "mount-read-only": _InspectMutation("configured-mount", "ReadOnly", False),
    "actual-mount-type": _InspectMutation("actual-mount", "Type", "bind"),
    "actual-volume-name": _InspectMutation("actual-mount", "Name", "attacker-volume"),
    "actual-volume-source": _InspectMutation(
        "actual-mount",
        "Source",
        f"/var/lib/docker/volumes/{RUNS_VOLUME_NAME}/_data/run-1/workspace",
    ),
    "actual-volume-destination": _InspectMutation("actual-mount", "Destination", "/workspace-shadow"),
    "actual-volume-driver": _InspectMutation("actual-mount", "Driver", "hostile"),
    "actual-volume-rw": _InspectMutation("actual-mount", "RW", True),
    "log-config": _InspectMutation("host", "LogConfig", {"Type": "json-file", "Config": {}}),
    "log-compress": _InspectMutation(
        "host",
        "LogConfig",
        {"Type": "local", "Config": {"compress": "true", "max-file": "1", "max-size": "1m"}},
    ),
    "shm-size": _InspectMutation("host", "ShmSize", 1024 * 1024 * 1024),
    "labels": _InspectMutation("labels", SANDBOX_SCOPE_LABEL, "other-scope"),
}


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _first_json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, list)
    assert value
    return _json_object(value[0])


def _inspect_mutation_target(inspect: JsonObject, target: InspectMutationTarget) -> JsonObject:
    config = _json_object(inspect["Config"])
    host = _json_object(inspect["HostConfig"])
    if target == "config":
        return config
    if target == "host":
        return host
    if target == "labels":
        return _json_object(config["Labels"])
    if target == "actual-mount":
        return _first_json_object(inspect["Mounts"])
    configured_mount = _first_json_object(host["Mounts"])
    if target == "configured-mount":
        return configured_mount
    return _json_object(configured_mount["VolumeOptions"])


def _apply_inspect_mutation(inspect: JsonObject, tamper: str) -> None:
    mutation = _INSPECT_MUTATIONS[tamper]
    target = _inspect_mutation_target(inspect, mutation.target)
    target[mutation.key] = mutation.value


class _FakeEngine:
    def __init__(
        self,
        *,
        stay_running: bool = False,
        report_mode: str = "valid",
        tamper_inspect: Callable[[JsonObject], None] | None = None,
        tamper_volume: Callable[[JsonObject], None] | None = None,
        create_response_lost: bool = False,
        remove_fails: bool = False,
        leave_label_residue: bool = False,
    ) -> None:
        self.stay_running = stay_running
        self.report_mode = report_mode
        self.tamper_inspect = tamper_inspect
        self.tamper_volume = tamper_volume
        self.create_response_lost = create_response_lost
        self.remove_fails = remove_fails
        self.leave_label_residue = leave_label_residue
        self.config: JsonObject | None = None
        self.container_name: str | None = None
        self.started = False
        self.killed = False
        self.removed = False
        self.label_queries: list[str] = []
        self.volume_queries: list[str] = []
        self.report_bytes: bytes | None = None

    def resolve_image_id(self, image_ref: str) -> str:
        assert image_ref == "agentgov-test-sandbox:dev"
        return IMAGE_ID

    def create_container(self, *, name: str, config: JsonObject) -> str:
        self.container_name = name
        self.config = copy.deepcopy(config)
        if self.create_response_lost:
            raise DockerEngineError(code="DOCKER_ENGINE_UNAVAILABLE", operation="container create")
        return CONTAINER_ID

    def inspect_container(self, container_id: str) -> JsonObject:
        assert container_id == CONTAINER_ID
        assert self.config is not None
        config = copy.deepcopy(self.config)
        host = config.pop("HostConfig")
        assert isinstance(host, dict)
        configured_mounts = host.get("Mounts")
        assert isinstance(configured_mounts, list)
        actual_mounts: list[JsonObject] = []
        for raw_mount in configured_mounts:
            assert isinstance(raw_mount, dict)
            actual_mounts.append(
                {
                    "Type": raw_mount["Type"],
                    "Name": raw_mount["Source"],
                    "Source": f"/var/lib/docker/volumes/{raw_mount['Source']}/_data",
                    "Destination": raw_mount["Target"],
                    "Driver": "local",
                    "RW": not bool(raw_mount["ReadOnly"]),
                    "Propagation": "",
                }
            )
        running = self.started and self.stay_running and not self.killed
        exit_code = 137 if self.killed else 0
        inspect: JsonObject = {
            "Config": config,
            "HostConfig": host,
            "Mounts": actual_mounts,
            "State": {"Running": running, "ExitCode": exit_code},
        }
        if self.tamper_inspect is not None:
            self.tamper_inspect(inspect)
        return inspect

    def inspect_volume(self, volume_name: str) -> JsonObject:
        assert volume_name == RUNS_VOLUME_NAME
        self.volume_queries.append(volume_name)
        inspect: JsonObject = {
            "Name": volume_name,
            "Driver": "local",
            "Options": None,
            "Mountpoint": f"/var/lib/docker/volumes/{volume_name}/_data",
            "Scope": "local",
        }
        if self.tamper_volume is not None:
            self.tamper_volume(inspect)
        return inspect

    def start_container(self, container_id: str) -> None:
        assert container_id == CONTAINER_ID
        self.started = True
        if self.report_mode == "valid":
            self.report_bytes = json.dumps({"exit_code": 0, "items": [{"nodeid": "tests/test_safe.py::test_safe", "outcome": "passed"}]}).encode()
        elif self.report_mode == "backend-field-forgery":
            self.report_bytes = json.dumps(
                {
                    "exit_code": 0,
                    "items": [{"nodeid": "tests/test_safe.py::test_safe", "outcome": "passed"}],
                    "status": "passed",
                    "worker_id": "forged-worker",
                    "score": 100,
                }
            ).encode()
        elif self.report_mode == "empty-pass":
            self.report_bytes = json.dumps({"exit_code": 0, "items": []}).encode()
        elif self.report_mode == "malformed-envelope":
            self.report_bytes = b"{}"

    def kill_container(self, container_id: str) -> None:
        assert container_id == CONTAINER_ID
        self.killed = True

    def remove_container(self, container_id: str) -> None:
        assert container_id == CONTAINER_ID
        if self.remove_fails:
            raise DockerEngineError(code="DOCKER_ENGINE_REQUEST_FAILED", operation="container remove", status_code=500)
        self.removed = True

    def container_ids_with_label(self, label: str) -> tuple[str, ...]:
        self.label_queries.append(label)
        return (CONTAINER_ID,) if self.leave_label_residue or not self.removed else ()

    def read_container_logs(
        self,
        container_id: str,
        *,
        max_bytes_per_stream: int,
        tail_lines: int | None = None,
        include_stderr: bool = True,
    ) -> DockerLogs:
        assert container_id == CONTAINER_ID
        if tail_lines is not None:
            assert tail_lines == 1
            assert include_stderr is False
            if self.report_mode == "missing" or self.report_bytes is None:
                return DockerLogs(stdout="", stderr="", truncated=False)
            payload = self.report_bytes if self.report_mode != "malformed-envelope" else b"not-json"
            return DockerLogs(
                stdout=f"{WORKSPACE_REPORT_LOG_PREFIX}{payload.decode('utf-8')}\n",
                stderr="",
                truncated=False,
            )
        assert max_bytes_per_stream == MAX_CAPTURED_OUTPUT_BYTES
        report_line = "" if self.report_bytes is None else f"\n{WORKSPACE_REPORT_LOG_PREFIX}{self.report_bytes.decode('utf-8')}\n"
        return DockerLogs(stdout=f"pytest stdout{report_line}", stderr="pytest stderr", truncated=False)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _spec(tmp_path: Path, *, run_id: str = "run-1") -> SandboxRunSpec:
    workspace = tmp_path / run_id / "workspace"
    workspace.mkdir(parents=True)
    return SandboxRunSpec(
        run_id=run_id,
        scope_id="scope-1",
        agent_id="security-operations-expert",
        commit_sha="c" * 40,
        workspace_path=workspace,
        runs_volume_name=RUNS_VOLUME_NAME,
        image_ref="agentgov-test-sandbox:dev",
        timeout_seconds=30,
    )


def _execute(engine: _FakeEngine, spec: SandboxRunSpec, **kwargs: object):
    executor = ContainerSandboxExecutor(engine=engine, **kwargs)
    bound: list[str] = []
    result = executor.execute(spec, cancel_requested=lambda: False, on_container_created=bound.append)
    assert bound == [CONTAINER_ID]
    return result


def test_executor_binds_before_start_and_proves_fixed_isolation(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine()
    callback_observation: list[tuple[str, bool]] = []
    executor = ContainerSandboxExecutor(engine=engine)

    result = executor.execute(
        spec,
        cancel_requested=lambda: False,
        on_container_created=lambda container_id: callback_observation.append((container_id, engine.started)),
    )

    assert callback_observation == [(CONTAINER_ID, False)]
    assert result.error_code is None
    assert result.exit_code == 0
    assert result.report == {
        "exit_code": 0,
        "items": [
            {
                "nodeid": "tests/test_safe.py::test_safe",
                "outcome": "passed",
                "duration_seconds": 0.0,
                "phase": "call",
                "detail": None,
            }
        ],
        "invocations": [],
    }
    assert result.stdout == "pytest stdout\n"
    assert result.isolation is not None
    assert result.isolation.pid_mode == "private"
    assert result.isolation.ipc_mode == "private"
    assert result.isolation.uts_mode == "private"
    assert result.isolation.auto_remove is False
    assert result.isolation.ports_published is False
    assert result.isolation.log_driver == "local"
    assert result.isolation.mounts[0].model_dump(mode="json") == {
        "target": "/workspace",
        "read_only": True,
        "mount_type": "volume",
        "source_scope": "run_workspace_subpath",
    }
    assert result.cleanup.complete is True
    assert engine.config is not None
    assert engine.config["Image"] == IMAGE_ID
    assert tuple(engine.config["Cmd"]) == FIXED_PYTEST_COMMAND
    assert engine.config["User"] == SANDBOX_USER
    environment = engine.config["Env"]
    assert isinstance(environment, list)
    assert all(isinstance(item, str) for item in environment)
    assert set(environment) == {f"{key}={value}" for key, value in FIXED_SANDBOX_ENV.items()}
    assert all("SECRET" not in item and "API_KEY" not in item for item in environment if isinstance(item, str))
    labels = engine.config["Labels"]
    assert isinstance(labels, dict)
    assert labels[SANDBOX_KIND_LABEL] == "true"
    assert labels[SANDBOX_RUN_LABEL] == "run-1"
    assert labels[SANDBOX_SCOPE_LABEL] == "scope-1"
    host = engine.config["HostConfig"]
    assert isinstance(host, dict)
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Privileged"] is False
    assert host["Devices"] == []
    assert host["PidsLimit"] == SANDBOX_PIDS_LIMIT
    assert host["Memory"] == SANDBOX_MEMORY_BYTES
    assert host["MemorySwap"] == SANDBOX_MEMORY_BYTES
    assert host["NanoCpus"] == SANDBOX_NANO_CPUS
    assert host["LogConfig"] == {
        "Type": "local",
        "Config": {"compress": "false", "max-file": "1", "max-size": "1m"},
    }
    assert engine.label_queries == [f"{SANDBOX_RUN_LABEL}=run-1"] * 2
    assert engine.volume_queries == [RUNS_VOLUME_NAME]


def test_executor_uses_only_the_read_only_run_volume_workspace_subpath(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine()

    result = _execute(engine, spec)

    assert result.report is not None
    assert engine.config is not None
    host = engine.config["HostConfig"]
    assert isinstance(host, dict)
    mounts = host["Mounts"]
    assert isinstance(mounts, list)
    assert mounts == [
        {
            "Type": "volume",
            "Source": RUNS_VOLUME_NAME,
            "Target": "/workspace",
            "ReadOnly": True,
            "VolumeOptions": {"NoCopy": True, "Subpath": "run-1/workspace"},
        }
    ]
    assert str(spec.workspace_path) not in json.dumps(engine.config)
    assert host["Tmpfs"] == {
        "/output": f"rw,noexec,nosuid,nodev,size={64 * 1024 * 1024},mode=1777",
        "/tmp": f"rw,noexec,nosuid,nodev,size={64 * 1024 * 1024},mode=1777",
    }


@pytest.mark.parametrize("volume_name", ["/host/path", "../escape", "volume/name", "bad\nname", ""])
def test_sandbox_spec_rejects_host_paths_and_invalid_volume_names(tmp_path: Path, volume_name: str) -> None:
    with pytest.raises(ValueError, match="volume name"):
        replace(_spec(tmp_path), runs_volume_name=volume_name)


def test_executor_rejects_worker_workspace_from_a_different_run(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    other_workspace = tmp_path / "run-2" / "workspace"
    other_workspace.mkdir(parents=True)
    mismatched = replace(spec, workspace_path=other_workspace)
    engine = _FakeEngine()

    with pytest.raises(SandboxExecutorError, match="selected run directory") as error:
        ContainerSandboxExecutor(engine=engine).execute(
            mismatched,
            cancel_requested=lambda: False,
            on_container_created=lambda _container_id: None,
        )

    assert error.value.code == "SANDBOX_PATH_MAPPING_INVALID"
    assert engine.config is None


def test_inspect_mismatch_never_starts_but_still_cleans(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    def tamper(inspect: JsonObject) -> None:
        host = inspect["HostConfig"]
        assert isinstance(host, dict)
        host["NetworkMode"] = "bridge"

    engine = _FakeEngine(tamper_inspect=tamper)

    result = _execute(engine, spec)

    assert result.error_code == "SANDBOX_INSPECT_MISMATCH"
    assert result.isolation is None
    assert "network" in result.isolation_failed_checks
    assert engine.started is False
    assert result.cleanup.complete is True


def test_inspect_rejects_inherited_environment_value_with_url_credentials(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    def inject_credential(inspect: JsonObject) -> None:
        config = inspect["Config"]
        assert isinstance(config, dict)
        environment = config["Env"]
        assert isinstance(environment, list)
        environment.append("PYTHON_GET_PIP_URL=https://build-user:build-secret@example.invalid/simple")

    engine = _FakeEngine(tamper_inspect=inject_credential)

    result = _execute(engine, spec)

    assert result.error_code == "SANDBOX_INSPECT_MISMATCH"
    assert result.isolation is None
    assert result.isolation_failed_checks == ("environment",)
    assert engine.started is False
    assert result.cleanup.complete is True


@pytest.mark.parametrize("tamper", tuple(_INSPECT_MUTATIONS))
def test_inspect_rejects_image_or_daemon_authority_expansion(tmp_path: Path, tamper: str) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine(tamper_inspect=lambda inspect: _apply_inspect_mutation(inspect, tamper))

    result = _execute(engine, spec)

    assert result.error_code == "SANDBOX_INSPECT_MISMATCH"
    assert result.isolation is None
    assert engine.started is False
    assert result.cleanup.complete is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Name", "attacker-volume"),
        ("Driver", "hostile"),
        ("Scope", "global"),
        ("Options", {"type": "none", "device": "/host"}),
        ("Mountpoint", "/var/lib/docker/volumes/attacker-volume/_data"),
    ],
)
def test_volume_inspect_mismatch_never_signs_or_starts(tmp_path: Path, field: str, value: object) -> None:
    def tamper(volume: JsonObject) -> None:
        volume[field] = value  # type: ignore[literal-required]

    engine = _FakeEngine(tamper_volume=tamper)

    result = _execute(engine, _spec(tmp_path))

    assert result.error_code == "SANDBOX_INSPECT_MISMATCH"
    assert result.isolation is None
    assert ("mounts" if field == "Mountpoint" else "volume") in result.isolation_failed_checks
    assert engine.started is False
    assert result.cleanup.complete is True


def test_container_binding_failure_never_starts_and_still_cleans(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine()

    def reject_claim(_container_id: str) -> None:
        raise SandboxExecutorError(code="AGENT_TEST_WORKER_CLAIM_LOST", message="worker claim no longer belongs to this worker")

    result = ContainerSandboxExecutor(engine=engine).execute(
        spec,
        cancel_requested=lambda: False,
        on_container_created=reject_claim,
    )

    assert result.error_code == "AGENT_TEST_WORKER_CLAIM_LOST"
    assert engine.started is False
    assert result.cleanup.complete is True


def test_lost_create_response_is_recovered_by_run_label_cleanup(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine(create_response_lost=True)
    callbacks: list[str] = []

    result = ContainerSandboxExecutor(engine=engine).execute(
        spec,
        cancel_requested=lambda: False,
        on_container_created=callbacks.append,
    )

    assert callbacks == []
    assert result.container_id is None
    assert result.error_code == "DOCKER_ENGINE_UNAVAILABLE"
    assert result.cleanup.complete is True
    assert engine.removed is True


def test_timeout_kills_then_ingests_and_cleans(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    spec = replace(spec, timeout_seconds=2)
    engine = _FakeEngine(stay_running=True, report_mode="missing")
    clock = _Clock()

    result = _execute(engine, spec, poll_interval_seconds=1, clock=clock, sleep=clock.advance)

    assert result.timed_out is True
    assert result.cancelled is False
    assert result.exit_code == 137
    assert result.report is None
    assert result.error_code is None
    assert engine.killed is True
    assert result.cleanup.complete is True


def test_poll_cancel_kills_then_cleans(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine(stay_running=True, report_mode="missing")
    calls = 0

    def cancel_requested() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    result = ContainerSandboxExecutor(engine=engine).execute(
        spec,
        cancel_requested=cancel_requested,
        on_container_created=lambda _container_id: None,
    )

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.report is None
    assert result.error_code is None
    assert engine.killed is True
    assert result.cleanup.complete is True


@pytest.mark.parametrize("report_mode", ["missing", "malformed-envelope"])
def test_missing_or_unsafe_report_forces_error_and_cleanup(tmp_path: Path, report_mode: str) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine(report_mode=report_mode)

    result = _execute(engine, spec)

    assert result.exit_code == 0
    assert result.error_code == "SANDBOX_REPORT_INVALID"
    assert result.report is None
    assert result.cleanup.complete is True


@pytest.mark.parametrize("report_mode", ["backend-field-forgery", "empty-pass"])
def test_agent_owned_report_cannot_inject_backend_facts_or_claim_an_empty_pass(tmp_path: Path, report_mode: str) -> None:
    engine = _FakeEngine(report_mode=report_mode)

    result = _execute(engine, _spec(tmp_path))

    assert result.error_code == "SANDBOX_REPORT_INVALID"
    assert result.report is None
    assert result.exit_code == 0
    assert result.cleanup.complete is True


def test_cleanup_failure_overrides_successful_pytest(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    engine = _FakeEngine(remove_fails=True, leave_label_residue=True)

    result = _execute(engine, spec)

    assert result.exit_code == 0
    assert result.report is not None
    assert result.error_code == "SANDBOX_CLEANUP_FAILED"
    assert result.cleanup.removed is False
    assert result.cleanup.labels_empty is False


def _docker_frame(stream: int, payload: bytes) -> bytes:
    return bytes((stream, 0, 0, 0)) + len(payload).to_bytes(4, byteorder="big") + payload


@pytest.mark.parametrize(
    "content_type",
    ["application/vnd.docker.multiplexed-stream", "application/vnd.docker.raw-stream"],
)
def test_docker_engine_decodes_multiple_stdout_and_stderr_frames(content_type: str) -> None:
    wire = _docker_frame(1, b"out-1") + _docker_frame(2, b"err") + _docker_frame(1, b"-out-2")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/containers/{CONTAINER_ID}/logs"
        return httpx.Response(200, headers={"content-type": content_type}, content=wire)

    http_client = httpx.Client(base_url="http://docker", transport=httpx.MockTransport(handler))
    engine = DockerEngineClient(client=http_client)

    logs = engine.read_container_logs(CONTAINER_ID, max_bytes_per_stream=64)

    assert logs.stdout == "out-1-out-2"
    assert logs.stderr == "err"
    assert logs.truncated is False
    http_client.close()


def test_docker_engine_can_tail_only_the_stdout_report_channel() -> None:
    wire = _docker_frame(1, f'{WORKSPACE_REPORT_LOG_PREFIX}{{"exit_code":0}}\n'.encode())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["stdout"] == "1"
        assert request.url.params["stderr"] == "0"
        assert request.url.params["tail"] == "1"
        return httpx.Response(200, headers={"content-type": "application/vnd.docker.multiplexed-stream"}, content=wire)

    http_client = httpx.Client(base_url="http://docker", transport=httpx.MockTransport(handler))
    engine = DockerEngineClient(client=http_client)

    logs = engine.read_container_logs(
        CONTAINER_ID,
        max_bytes_per_stream=MAX_CAPTURED_OUTPUT_BYTES,
        tail_lines=1,
        include_stderr=False,
    )

    assert logs.stdout.startswith(WORKSPACE_REPORT_LOG_PREFIX)
    assert logs.stderr == ""
    http_client.close()


def test_docker_engine_bounds_multiplexed_logs_per_stream() -> None:
    wire = _docker_frame(1, b"a" * 32) + _docker_frame(2, b"error")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/vnd.docker.raw-stream"}, content=wire)

    http_client = httpx.Client(base_url="http://docker", transport=httpx.MockTransport(handler))
    engine = DockerEngineClient(client=http_client)

    logs = engine.read_container_logs(CONTAINER_ID, max_bytes_per_stream=8)

    assert logs.stdout.startswith("aaaaaaaa")
    assert logs.stdout.endswith("[output truncated]")
    assert logs.stderr == "error"
    assert logs.truncated is True
    http_client.close()


def test_docker_engine_rejects_mutable_or_malformed_image_identity() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Id": "agentgov-test-sandbox:dev"})

    http_client = httpx.Client(base_url="http://docker", transport=httpx.MockTransport(handler))
    engine = DockerEngineClient(client=http_client)

    with pytest.raises(DockerEngineProtocolError) as error:
        engine.resolve_image_id("agentgov-test-sandbox:dev")

    assert error.value.code == "DOCKER_IMAGE_ID_INVALID"
    http_client.close()


def test_docker_engine_inspects_current_worker_by_unambiguous_short_container_id() -> None:
    short_id = CONTAINER_ID[:12]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/containers/{short_id}/json"
        return httpx.Response(200, json={"Id": CONTAINER_ID, "Mounts": []})

    http_client = httpx.Client(base_url="http://docker", transport=httpx.MockTransport(handler))
    engine = DockerEngineClient(client=http_client)

    assert engine.inspect_container(short_id)["Id"] == CONTAINER_ID
    http_client.close()


def test_docker_engine_inspects_only_a_valid_exact_volume_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/volumes/{RUNS_VOLUME_NAME}"
        return httpx.Response(200, json={"Name": RUNS_VOLUME_NAME, "Driver": "local"})

    http_client = httpx.Client(base_url="http://docker", transport=httpx.MockTransport(handler))
    engine = DockerEngineClient(client=http_client)

    assert engine.inspect_volume(RUNS_VOLUME_NAME)["Name"] == RUNS_VOLUME_NAME
    with pytest.raises(ValueError, match="volume name"):
        engine.inspect_volume("../../host-path")
    http_client.close()
