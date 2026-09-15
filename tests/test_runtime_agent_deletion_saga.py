from __future__ import annotations

import asyncio
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict, harness_digest
from app.services.agent_governance import AgentGovernanceService
from app.services.runtime_agent_deletion import RuntimeAgentDeletionService

from business_agent_test_utils import create_test_business_agent_workspace


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def unavailable_runtime_endpoint() -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        listener.close()


@pytest.fixture
def deletion_resources(tmp_path: Path, unavailable_runtime_endpoint: str):
    data_dir = tmp_path / "data"
    workspace = data_dir / "business-agents" / "agent-a" / "workspace"
    create_test_business_agent_workspace(workspace, agent_id="agent-a", name="Agent A")
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.local")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "published")
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=workspace.parent / "version" / "worktrees",
        releases_dir=workspace.parent / "version" / "releases",
    )
    commit = versions.inspect_clean_head()[0]
    digest = harness_digest(workspace)
    factory = make_session_factory(data_dir / "runtime.sqlite3")
    registry = AgentRegistryStore(factory)
    record = registry.create_business_agent(name="Agent A", agent_id="agent-a", workspace_dir=str(workspace))
    store = RuntimeRunStore(factory)
    registry.deletion_pending = store.agent_deletion_pending
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id=commit,
        digest=digest,
        runtime_agent_id="runtime-a",
    )
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id=commit,
        runtime_agent_id="runtime-a",
        digest=digest,
    )
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": [{"type": "text", "text": "retain history"}]},
        entities={},
        metadata={},
    )
    retained_run = store.fail_trigger(run.run_id, error={"type": "network-unavailable"})
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "candidates")
    snapshot = snapshots.materialize(
        version_store=versions,
        agent_id="agent-a",
        agent_version_id=commit,
        expected_digest=digest,
    )
    feedback = FeedbackStore(data_dir=data_dir, workspace_dir=tmp_path / "governor")
    governance = AgentGovernanceService(
        feedback_store=feedback,
        agent_version_store=versions,
    )
    client = AgentScopeRuntimeClient(
        unavailable_runtime_endpoint,
        shared_secret="test-only-runtime-shared-secret",
        timeout_seconds=1,
    )
    service = RuntimeAgentDeletionService(
        client=client,
        store=store,
        registry=registry,
        snapshots=snapshots,
        data_dir=data_dir,
        evict_agent_store=governance.evict_agent_store,
    )
    try:
        yield record, store, registry, snapshot, client, service, retained_run, governance
    finally:
        asyncio.run(client.close())


def test_deletion_start_persists_exact_generation_without_touching_resources(deletion_resources) -> None:
    record, store, registry, snapshot, _client, service, retained_run, governance = deletion_resources

    with governance.version_maintenance.lease(
        agent_id="agent-a",
        kind="agent_delete",
        owner_id="pytest:deletion-start",
    ):
        intent = service.start(record)

    persisted = store.get_agent_deletion(intent.intent_id)
    assert persisted.status == "cleanup_pending"
    assert persisted.agent_id == "agent-a"
    assert persisted.agent_generation == record.created_at
    assert registry.get_agent("agent-a") is not None
    assert snapshot.workspace.exists()
    assert Path(record.workspace_dir).exists()
    assert store.get_run(retained_run.run_id).run_id == retained_run.run_id


def test_real_runtime_network_failure_is_durable_and_redacted(deletion_resources) -> None:
    record, store, registry, snapshot, _client, service, retained_run, governance = deletion_resources
    with governance.version_maintenance.lease(
        agent_id="agent-a",
        kind="agent_delete",
        owner_id="pytest:network-failure",
    ) as lease:
        intent = service.start(record)
        result = asyncio.run(service.resume(intent.intent_id, assert_maintenance_active=lease.assert_active))

    assert result.cleanup_complete is False
    pending = store.get_agent_deletion(intent.intent_id)
    assert pending.status == "cleanup_pending"
    assert pending.tombstoned is True
    assert pending.error_json == {
        "stage": "enumerate_runtime_sessions",
        "error_type": "RuntimeUpstreamError",
    }
    assert registry.get_agent("agent-a") is None
    assert snapshot.workspace.exists()
    assert Path(record.workspace_dir).exists()
    assert store.get_run(retained_run.run_id).run_id == retained_run.run_id
    database_bytes = Path(record.workspace_dir).parents[2].joinpath("runtime.sqlite3").read_bytes()
    assert b"127.0.0.1" not in database_bytes
    assert b"test-only-runtime-shared-secret" not in database_bytes


def test_business_agent_deletion_waits_for_unlocated_ephemeral_provisioning(deletion_resources) -> None:
    record, store, registry, _snapshot, _client, service, retained_run, governance = deletion_resources
    store.start_ephemeral_resource(
        cache_key="candidate:in-flight",
        business_agent_id="agent-a",
        version_owner_id=f"candidate-{'a' * 48}",
        agent_version_id=retained_run.agent_version_id,
        digest=retained_run.harness_digest,
        source_id=f"candidate-{'a' * 48}",
        source_kind="candidate_snapshot",
        workspace_id=f"candidate-{'a' * 48}--v-{retained_run.harness_digest}",
    )

    with governance.version_maintenance.lease(
        agent_id="agent-a",
        kind="agent_delete",
        owner_id="pytest:in-flight-candidate",
    ):
        with pytest.raises(RuntimeStateConflict, match="provisioning must settle"):
            service.start(record)

    assert registry.get_agent("agent-a") is not None
    assert store.recoverable_agent_deletions() == []


class _PartialDeletionClient:
    """只在第二个 Session 首次删除时失败，用于验证持久化 saga 续跑。"""

    def __init__(self) -> None:
        self.sessions = {"runtime-a": {"session-a", "session-b"}}
        self.agents = {"runtime-a"}
        self.fail_session_once = "session-b"

    async def list_session_ids(self, runtime_agent_id: str) -> list[str]:
        if runtime_agent_id not in self.agents:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        return sorted(self.sessions.get(runtime_agent_id, set()))

    async def delete_session(self, session_id: str, runtime_agent_id: str) -> None:
        if self.fail_session_once == session_id:
            self.fail_session_once = None
            raise RuntimeUpstreamError(502, b'{"detail":"provider secret must not persist"}')
        sessions = self.sessions.get(runtime_agent_id, set())
        if session_id not in sessions:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        sessions.remove(session_id)

    async def delete_agent(self, runtime_agent_id: str) -> None:
        if runtime_agent_id not in self.agents:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        if self.sessions.get(runtime_agent_id):
            raise AssertionError("all Runtime Sessions must be deleted first")
        self.agents.remove(runtime_agent_id)


def test_partial_remote_delete_is_durable_and_restart_resumes_without_losing_run(
    deletion_resources,
) -> None:
    record, store, registry, snapshot, _client, original, retained_run, governance = deletion_resources
    store.bind_session(
        session_id="session-b",
        agent_id="agent-a",
        agent_version_id=retained_run.agent_version_id,
        runtime_agent_id="runtime-a",
        digest=retained_run.harness_digest,
    )
    runtime = _PartialDeletionClient()
    evicted: list[str] = []
    service = RuntimeAgentDeletionService(
        client=runtime,  # type: ignore[arg-type]
        store=store,
        registry=registry,
        snapshots=original.snapshots,
        data_dir=original.data_dir,
        evict_agent_store=evicted.append,
    )
    with governance.version_maintenance.lease(
        agent_id="agent-a",
        kind="agent_delete",
        owner_id="pytest:partial-delete",
    ) as lease:
        intent = service.start(record)
        partial = asyncio.run(
            service.resume(intent.intent_id, assert_maintenance_active=lease.assert_active),
        )

    assert partial.cleanup_complete is False
    pending = store.get_agent_deletion(intent.intent_id)
    assert pending.status == "cleanup_pending"
    assert pending.deleted_session_ids_json == ["session-a"]
    assert pending.error_json == {
        "stage": "delete_runtime_session",
        "error_type": "RuntimeUpstreamError",
    }
    assert "secret" not in str(pending.error_json)
    assert registry.get_agent("agent-a") is None
    assert snapshot.workspace.exists()
    assert store.get_run(retained_run.run_id).run_id == retained_run.run_id

    restarted_store = RuntimeRunStore(store.Session)
    restarted_registry = AgentRegistryStore(store.Session)
    restarted_registry.deletion_pending = restarted_store.agent_deletion_pending
    restarted = RuntimeAgentDeletionService(
        client=runtime,  # type: ignore[arg-type]
        store=restarted_store,
        registry=restarted_registry,
        snapshots=PublishedHarnessSnapshotStore(original.snapshots.root),
        data_dir=original.data_dir,
        evict_agent_store=evicted.append,
    )
    completed = asyncio.run(
        restarted.resume(intent.intent_id, assert_maintenance_active=lambda: None),
    )

    assert completed.cleanup_complete is True
    persisted = restarted_store.get_agent_deletion(intent.intent_id)
    assert persisted.status == "cleanup_complete"
    assert persisted.deleted_session_ids_json == ["session-a", "session-b"]
    assert persisted.deleted_runtime_agent_ids_json == ["runtime-a"]
    assert runtime.sessions["runtime-a"] == set()
    assert runtime.agents == set()
    assert evicted == ["agent-a"]
    assert not snapshot.workspace.parent.exists()
    assert restarted_store.get_run(retained_run.run_id).run_id == retained_run.run_id
