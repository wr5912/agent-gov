from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from app.agent_testing.execution_contracts import (
    FIXED_PYTEST_COMMAND,
    FIXED_SANDBOX_ENV,
    P0_EXACT_COMMIT_LANE,
    RECEIPT_CONTRACT,
    AgentTestCleanupReceipt,
    AgentTestExecutionReceipt,
    AgentTestInvocationReceipt,
    AgentTestIsolationReceipt,
    AgentTestResultReceipt,
    AgentTestSandboxMountReceipt,
    AgentTestTargetReceipt,
    canonical_json_digest,
    sandbox_environment_digest,
)

TerminalStatus = Literal["passed", "failed", "error", "cancelled", "interrupted"]
SourceObservation = Literal["stable", "changed"]


def _isolation_receipt() -> AgentTestIsolationReceipt:
    return AgentTestIsolationReceipt(
        user="65532:65532",
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
        pids_limit=256,
        memory_bytes=536870912,
        memory_swap_bytes=536870912,
        nano_cpus=1000000000,
        tmpfs_targets=("/output", "/tmp"),
        tmpfs_size_bytes=67108864,
        tmpfs_noexec=True,
        tmpfs_nosuid=True,
        tmpfs_nodev=True,
        shm_size_bytes=16777216,
        ports_published=False,
        auto_remove=False,
        restart_policy="no",
        log_driver="local",
        log_max_bytes=1048576,
        log_max_files=1,
        log_compression=False,
        docker_socket_mounted=False,
    )


def terminal_run(
    status: TerminalStatus,
    *,
    test_run_id: str = "atr-contract",
    agent_id: str = "agent-a",
    commit_sha: str = "a" * 40,
    error_code: str | None = None,
    source_observation: SourceObservation = "stable",
) -> dict[str, object]:
    exit_code = 0 if status == "passed" else 1 if status == "failed" else 137
    items: list[dict[str, object]] = []
    if status == "passed":
        items = [{"nodeid": "tests/test_safe.py::test_safe", "outcome": "passed", "phase": "call"}]
    elif status == "failed":
        items = [{"nodeid": "tests/test_failure.py::test_failure", "outcome": "failed", "phase": "call"}]
    report: dict[str, object] = {"exit_code": exit_code, "items": items}
    source_digest = "b" * 64
    post_source_digest = source_digest if source_observation == "stable" else "9" * 64
    suite_digest = "c" * 64
    tree_sha = "d" * 40
    receipt = AgentTestExecutionReceipt(
        contract=RECEIPT_CONTRACT,
        lane=P0_EXACT_COMMIT_LANE,
        assurance_level="execution_provenance",
        test_run_id=test_run_id,
        worker_id="worker-test",
        container_id="e" * 64,
        target=AgentTestTargetReceipt(
            agent_id=agent_id,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            source_digest=source_digest,
            pre_source_digest=source_digest,
            post_source_digest=post_source_digest,
            source_observation=source_observation,
            suite_digest=suite_digest,
        ),
        invocation=AgentTestInvocationReceipt(
            image_id=f"sha256:{'f' * 64}",
            argv=FIXED_PYTEST_COMMAND,
            environment_keys=tuple(sorted(FIXED_SANDBOX_ENV)),
            environment_digest=sandbox_environment_digest(),
            working_directory="/workspace",
        ),
        isolation=_isolation_receipt(),
        result=AgentTestResultReceipt(
            status=status,
            exit_code=exit_code,
            duration_ms=10,
            workspace_report_authority="agent_owned_unverified",
            workspace_report_digest=canonical_json_digest(report),
            stdout_digest=canonical_json_digest(""),
            stderr_digest=canonical_json_digest(""),
        ),
        cleanup=AgentTestCleanupReceipt(
            container_removed=True,
            label_residue_absent=True,
            temporary_paths_removed=True,
            error_codes=(),
        ),
    ).with_digest()
    return {
        "test_run_id": test_run_id,
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "status": status,
        "exit_code": exit_code,
        "suite_digest": suite_digest,
        "source_digest": source_digest,
        "source_tree_sha": tree_sha,
        "report": report,
        "items": items,
        "stdout": "",
        "stderr": "",
        "error": {"error_code": error_code} if error_code else {},
        "receipt": receipt.model_dump(mode="json"),
    }


@dataclass(frozen=True)
class WorkerRuntimeEvidence:
    volume_name: str
    mountpoint: Path
    worker: dict[str, object]
    volume: dict[str, object]


def worker_runtime_evidence(
    *,
    acceptance_root: Path,
    worker_container: str,
    acceptance_run_id: str,
    acceptance_image_label: str,
    hostile: str | None,
) -> WorkerRuntimeEvidence:
    volume_name = "agentgov-test-current_agent-test-runs"
    mountpoint = Path(f"/var/lib/docker/volumes/{volume_name}/_data")
    worker: dict[str, object] = {
        "Id": "a" * 64,
        "Name": f"/{worker_container}",
        "State": {"Running": hostile != "stopped"},
        "Config": {
            "Labels": {
                acceptance_image_label: "stale" if hostile == "stale-label" else acceptance_run_id,
            }
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(acceptance_root / ("other-data" if hostile == "data-source" else "data")),
                "Destination": "/data",
                "RW": True,
                "Propagation": "rprivate",
            },
            {
                "Type": "volume",
                "Name": volume_name,
                "Source": str(mountpoint),
                "Destination": "/agent-test-runs",
                "Driver": "local",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
                "Propagation": "rprivate",
            },
        ],
    }
    if hostile == "duplicate-volume":
        mounts = worker["Mounts"]
        assert isinstance(mounts, list)
        mounts.append(
            {
                "Type": "volume",
                "Name": "hostile-volume",
                "Source": "/var/lib/docker/volumes/hostile-volume/_data",
                "Destination": "/hostile",
                "Driver": "local",
                "RW": True,
            }
        )
    volume: dict[str, object] = {
        "Name": volume_name,
        "Driver": "local",
        "Scope": "local",
        "Options": {"device": "/host"} if hostile == "volume-options" else None,
        "Mountpoint": f"{mountpoint}/wrong" if hostile == "mountpoint" else str(mountpoint),
    }
    return WorkerRuntimeEvidence(
        volume_name=volume_name,
        mountpoint=mountpoint,
        worker=worker,
        volume=volume,
    )


@dataclass
class SourceChangedApiEvidence:
    test_run_id: str
    commit_sha: str
    terminal: dict[str, object]
    calls: list[tuple[str, str]] = field(default_factory=list)
    get_count: int = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/agent-test-runs":
            return httpx.Response(202, json={"test_run_id": self.test_run_id})
        if request.method == "GET" and request.url.path == f"/api/agent-test-runs/{self.test_run_id}":
            self.get_count += 1
            payload = {"status": "running"} if self.get_count == 1 else self.terminal
            return httpx.Response(200, json=payload)
        if request.method == "GET" and request.url.path == "/api/agent-test-runs/history":
            assert request.url.params["status"] == "passed"
            assert request.url.params["commit_sha"] == self.commit_sha
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


@dataclass
class SourceChangedDockerEvidence:
    sandbox_run_label: str
    projected_bytes: bytes
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return "acceptance-current"
        if command[1:3] == ["ps", "-aq"]:
            return ""
        if command[1:3] == ["ps", "-q"] and self.sandbox_run_label in " ".join(command):
            return "sandbox-container"
        if command[1:3] == ["ps", "-q"]:
            return "worker-container"
        if command[1] == "exec":
            if command[-1] == self.projected_bytes.hex():
                return hashlib.sha256(self.projected_bytes).hexdigest()
            return "absent"
        raise AssertionError(f"unexpected Docker evidence command: {command[:3]}")


def worker_evidence_runner(
    evidence: WorkerRuntimeEvidence,
    commands: list[list[str]],
    command: list[str],
) -> str:
    commands.append(command)
    payload = evidence.volume if command[1:3] == ["volume", "inspect"] else evidence.worker
    return json.dumps([payload])
