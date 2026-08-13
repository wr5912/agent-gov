from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent_testing import worker as worker_module
from app.agent_testing.container_executor import (
    SandboxCleanupResult,
    SandboxExecutionResult,
    SandboxRunSpec,
)
from app.agent_testing.execution_contracts import (
    FIXED_PYTEST_COMMAND,
    SANDBOX_LOG_MAX_BYTES,
    SANDBOX_LOG_MAX_FILES,
    SANDBOX_MEMORY_BYTES,
    SANDBOX_NANO_CPUS,
    SANDBOX_PIDS_LIMIT,
    SANDBOX_SHM_SIZE_BYTES,
    SANDBOX_TMPFS_SIZE_BYTES,
    SANDBOX_USER,
    AgentTestExecutionReceipt,
    AgentTestIsolationReceipt,
    AgentTestSandboxMountReceipt,
    verify_receipt_integrity,
)
from app.agent_testing.materializer import SourceFingerprint
from app.agent_testing.store import AgentTestingStore
from app.agent_testing.worker import AgentTestWorkerSettings
from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir

AGENT_ID = "agent-a"
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
SOURCE_CONTENT = b"# test Agent\n"


def source_digest(content: bytes) -> str:
    digest = hashlib.sha256(b"agentgov-source-v1\0")
    for value in (b"644", b"CLAUDE.md", str(len(content)).encode("ascii"), content):
        digest.update(value)
        digest.update(b"\0")
    return digest.hexdigest()


SOURCE_DIGEST = source_digest(SOURCE_CONTENT)
SUITE_DIGEST = "d" * 64
IMAGE_ID = f"sha256:{'e' * 64}"
CONTAINER_ID = "f" * 64
WORKER_CONTAINER_ID = "1" * 64
DAEMON_DATA_ROOT = Path("/daemon/agentgov/data")
RUNS_VOLUME_NAME = "agent-gov_agent-test-runs"
RUNS_VOLUME_MOUNTPOINT = Path(f"/var/lib/docker/volumes/{RUNS_VOLUME_NAME}/_data")


@dataclass
class FakeEngine:
    settings: AgentTestWorkerSettings
    labels_by_container: dict[str, set[str]] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    data_source: Path = DAEMON_DATA_ROOT
    runs_volume_name: str = RUNS_VOLUME_NAME
    runs_mountpoint: Path = RUNS_VOLUME_MOUNTPOINT
    runs_mount_type: object = "volume"
    runs_mount_rw: object = True
    volume_inspected_name: object = RUNS_VOLUME_NAME
    volume_driver: object = "local"
    volume_options: object = None
    volume_scope: object = "local"
    volume_mountpoint: object = str(RUNS_VOLUME_MOUNTPOINT)
    extra_mounts: list[dict[str, object]] = field(default_factory=list)
    inspected_id: str = WORKER_CONTAINER_ID
    running: object = True

    def inspect_container(self, container_id: str) -> dict[str, object]:
        assert container_id == self.settings.container_id
        mounts: list[dict[str, object]] = [
            {
                "Type": "bind",
                "Source": str(self.data_source),
                "Destination": str(self.settings.data_dir),
                "RW": True,
                "Propagation": "rprivate",
            },
            {
                "Type": self.runs_mount_type,
                "Name": self.runs_volume_name,
                "Source": str(self.runs_mountpoint),
                "Destination": str(self.settings.runs_dir),
                "RW": self.runs_mount_rw,
                "Propagation": "",
            },
            *self.extra_mounts,
        ]
        return {
            "Id": self.inspected_id,
            "State": {"Running": self.running},
            "Mounts": mounts,
        }

    def inspect_volume(self, volume_name: str) -> dict[str, object]:
        assert volume_name == self.runs_volume_name
        return {
            "Name": self.volume_inspected_name,
            "Driver": self.volume_driver,
            "Options": self.volume_options,
            "Mountpoint": self.volume_mountpoint,
            "Scope": self.volume_scope,
        }

    def container_ids_with_label(self, label: str) -> tuple[str, ...]:
        return tuple(container_id for container_id, labels in self.labels_by_container.items() if label in labels)

    def remove_container(self, container_id: str) -> None:
        self.removed.append(container_id)
        self.labels_by_container.pop(container_id, None)


@dataclass
class FakeExecutor:
    store: AgentTestingStore
    result: SandboxExecutionResult
    cancel_before_result: bool = False
    workspace_mutator: Callable[[Path], None] | None = None
    specs: list[SandboxRunSpec] = field(default_factory=list)
    observed_claims: list[dict[str, object]] = field(default_factory=list)

    def execute(
        self,
        spec: SandboxRunSpec,
        *,
        cancel_requested: Callable[[], bool],
        on_container_created: Callable[[str], None],
    ) -> SandboxExecutionResult:
        self.specs.append(spec)
        running = next(item for item in self.store.running_runs() if item["test_run_id"] == spec.run_id)
        self.observed_claims.append(
            {
                "status": running["status"],
                "worker_id": running["_worker_id"],
                "claim_generation": running["_claim_generation"],
            }
        )
        on_container_created(self.result.container_id)
        if self.cancel_before_result:
            self.store.request_cancel(spec.run_id)
            assert cancel_requested() is True
        if self.workspace_mutator is not None:
            self.workspace_mutator(spec.workspace_path)
        return self.result


class NoCallExecutor:
    def execute(self, *_args: object, **_kwargs: object) -> SandboxExecutionResult:
        raise AssertionError("契约校验失败时不得启动 sandbox")


class RaisingExecutor:
    def execute(self, *_args: object, **_kwargs: object) -> SandboxExecutionResult:
        raise RuntimeError("host executor failed after pre-source observation")


@dataclass
class FailingCleanupEngine(FakeEngine):
    def remove_container(self, container_id: str) -> None:
        del container_id
        raise RuntimeError("cleanup unavailable")


def worker_settings(tmp_path: Path, *, worker_id: str = "worker-a") -> AgentTestWorkerSettings:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    runs_dir = tmp_path / "worker-runs"
    runs_dir.mkdir()
    mountinfo_path = tmp_path / "mountinfo"
    write_mountinfo(
        mountinfo_path,
        data_destination=data_dir,
        runs_destination=runs_dir,
        data_root=DAEMON_DATA_ROOT,
        runs_root=RUNS_VOLUME_MOUNTPOINT,
    )
    return AgentTestWorkerSettings(
        data_dir=data_dir,
        runs_dir=runs_dir,
        sandbox_image="agentgov-test-sandbox:dev",
        docker_socket=tmp_path / "docker.sock",
        timeout_seconds=30,
        poll_seconds=0.1,
        worker_id=worker_id,
        container_id=WORKER_CONTAINER_ID[:12],
        mountinfo_path=mountinfo_path,
    )


def write_mountinfo(
    path: Path,
    *,
    data_destination: Path,
    runs_destination: Path,
    data_root: Path,
    runs_root: Path,
) -> None:
    def escaped(value: Path) -> str:
        return str(value).replace("\\", "\\134").replace(" ", "\\040")

    path.write_text(
        "\n".join(
            (
                f"101 1 252:17 {escaped(data_root)} {escaped(data_destination)} rw,relatime - ext4 /dev/vdb1 rw",
                f"102 1 252:17 {escaped(runs_root)} {escaped(runs_destination)} rw,relatime - ext4 /dev/vdb1 rw",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def testing_store(settings: AgentTestWorkerSettings) -> AgentTestingStore:
    session_factory = make_session_factory(runtime_db_path_from_data_dir(settings.data_dir))
    with session_factory.begin() as db:
        if db.get(AgentRegistryModel, AGENT_ID) is None:
            db.add(
                AgentRegistryModel(
                    agent_id=AGENT_ID,
                    name="Agent A",
                    category="business",
                    workspace_dir=str(business_agent_layout(settings.data_dir, AGENT_ID).workspace),
                    provision_state="ready",
                    provision_completed_token="test-agent-a-instance",
                )
            )
    return AgentTestingStore(session_factory)


def queued_run(store: AgentTestingStore) -> dict[str, object]:
    return store.create_run(
        agent_id=AGENT_ID,
        commit_sha=COMMIT_SHA,
        change_set_id=None,
        source="manual",
        command=list(FIXED_PYTEST_COMMAND),
        suite={"test_files": ["tests/test_safe.py"], "requires_live_agent": False},
        suite_digest=SUITE_DIGEST,
        source_digest=SOURCE_DIGEST,
        source_tree_sha=TREE_SHA,
    )


def fingerprint(*, source_digest: str = SOURCE_DIGEST) -> SourceFingerprint:
    return SourceFingerprint(
        commit_sha=COMMIT_SHA,
        tree_sha=TREE_SHA,
        source_digest=source_digest,
        file_count=1,
        total_bytes=len(SOURCE_CONTENT),
    )


def install_materialization_contract(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_digest: str = SOURCE_DIGEST,
    suite_digest: str = SUITE_DIGEST,
    requires_live_agent: bool = False,
    written_content: bytes = SOURCE_CONTENT,
) -> None:
    def materialize(_repository: Path, commit_sha: str, destination: Path) -> SourceFingerprint:
        assert commit_sha == COMMIT_SHA
        destination.mkdir(parents=True)
        source = destination / "CLAUDE.md"
        source.write_bytes(written_content)
        source.chmod(0o644)
        return fingerprint(source_digest=source_digest)

    monkeypatch.setattr(worker_module, "materialize_git_commit", materialize)
    monkeypatch.setattr(
        worker_module,
        "inspect_agent_test_suite",
        lambda *_args, **_kwargs: SimpleNamespace(
            runnable=True,
            suite_digest=suite_digest,
            requires_live_agent=requires_live_agent,
        ),
    )


def isolation_receipt() -> AgentTestIsolationReceipt:
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
                mount_type="volume",
                source_scope="run_workspace_subpath",
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


def execution_result(
    *,
    exit_code: int | None = 0,
    report: dict[str, object] | None = None,
    timed_out: bool = False,
    cancelled: bool = False,
    cleanup: SandboxCleanupResult | None = None,
) -> SandboxExecutionResult:
    return SandboxExecutionResult(
        image_id=IMAGE_ID,
        container_id=CONTAINER_ID,
        exit_code=exit_code,
        duration_seconds=0.125,
        stdout="pytest stdout",
        stderr="",
        report=report if report is not None else {"exit_code": exit_code, "items": []},
        isolation=isolation_receipt(),
        isolation_failed_checks=(),
        cleanup=cleanup or SandboxCleanupResult(removed=True, labels_empty=True),
        timed_out=timed_out,
        cancelled=cancelled,
        error_code=None,
        error_message=None,
    )


def typed_receipt(run: dict[str, object]) -> AgentTestExecutionReceipt:
    receipt = AgentTestExecutionReceipt.model_validate(run["receipt"])
    assert verify_receipt_integrity(receipt) is True
    return receipt
