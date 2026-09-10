from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from app.runtime.errors import ConflictError
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime_gateway.client import RuntimeUpstreamError
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.provisioning import RuntimeAgentProvisioner
from app.runtime_gateway.store import RuntimeObjectNotFound, RuntimeRunStore, RuntimeStateConflict, harness_digest
from app.services.runtime_agent_deletion import RuntimeAgentDeletionService

from business_agent_test_utils import create_test_business_agent_workspace


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


class _RuntimeClient:
    def __init__(self) -> None:
        self.sessions = {"runtime-a": {"session-a", "session-b"}}
        self.agents = {"runtime-a"}
        self.fail_session_once = "session-b"
        self.calls: list[tuple[str, str]] = []

    async def list_session_ids(self, runtime_agent_id: str) -> list[str]:
        self.calls.append(("list", runtime_agent_id))
        if runtime_agent_id not in self.agents:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        return sorted(self.sessions.get(runtime_agent_id, set()))

    async def delete_session(self, session_id: str, runtime_agent_id: str) -> None:
        self.calls.append(("delete_session", session_id))
        if self.fail_session_once == session_id:
            self.fail_session_once = None
            raise RuntimeUpstreamError(502, b'{"detail":"provider secret must not persist"}')
        sessions = self.sessions.get(runtime_agent_id, set())
        if session_id not in sessions:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        sessions.remove(session_id)

    async def delete_agent(self, runtime_agent_id: str) -> None:
        self.calls.append(("delete_agent", runtime_agent_id))
        if runtime_agent_id not in self.agents:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        if self.sessions.get(runtime_agent_id):
            raise AssertionError("all Runtime Sessions must be deleted first")
        self.agents.remove(runtime_agent_id)


def _fixture(tmp_path: Path):
    data_dir = tmp_path / "data"
    workspace = data_dir / "business-agents" / "agent-a" / "workspace"
    create_test_business_agent_workspace(workspace, agent_id="agent-a", name="Agent A")
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.local")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "published")

    from app.runtime.agent_git_store import GitAgentVersionStore

    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=workspace.parent / "version" / "worktrees",
        releases_dir=workspace.parent / "version" / "releases",
    )
    commit = versions.current_commit_sha()
    assert commit is not None
    digest = harness_digest(workspace)
    factory = make_session_factory(data_dir / "runtime.sqlite3")
    registry = AgentRegistryStore(factory)
    record = registry.create_business_agent(
        name="Agent A",
        agent_id="agent-a",
        workspace_dir=str(workspace),
    )
    store = RuntimeRunStore(factory)
    registry.deletion_pending = store.agent_deletion_pending
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id=commit,
        digest=digest,
        runtime_agent_id="runtime-a",
    )
    for session_id in ("session-a", "session-b"):
        store.bind_session(
            session_id=session_id,
            agent_id="agent-a",
            agent_version_id=commit,
            runtime_agent_id="runtime-a",
            digest=digest,
        )
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": [{"type": "text", "text": "retain history"}]},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    retained_run = store.fail_trigger(run.run_id, error={"type": "test"})
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "candidates")
    snapshot = snapshots.materialize(
        version_store=versions,
        agent_id="agent-a",
        agent_version_id=commit,
        expected_digest=digest,
    )
    client = _RuntimeClient()
    evicted: list[str] = []
    service = RuntimeAgentDeletionService(
        client=client,  # type: ignore[arg-type]
        store=store,
        registry=registry,
        snapshots=snapshots,
        data_dir=data_dir,
        evict_agent_store=evicted.append,
    )
    return record, versions, store, registry, snapshots, snapshot, client, service, retained_run, evicted


def test_partial_remote_delete_is_durable_and_restart_finishes_without_losing_history(tmp_path: Path) -> None:
    record, versions, store, registry, snapshots, snapshot, client, service, retained_run, evicted = _fixture(tmp_path)
    intent = service.start(record)

    partial = asyncio.run(service.resume(intent.intent_id, assert_maintenance_active=lambda: None))

    assert partial.cleanup_complete is False
    assert registry.get_agent("agent-a") is None
    pending = store.get_agent_deletion(intent.intent_id)
    assert pending.status == "cleanup_pending"
    assert pending.deleted_session_ids_json == ["session-a"]
    assert pending.error_json == {"stage": "delete_runtime_session", "error_type": "RuntimeUpstreamError"}
    assert "secret" not in str(pending.error_json)
    assert store.get_session("session-a").runtime_agent_id == "runtime-a"
    assert snapshot.workspace.exists()
    with pytest.raises(ConflictError, match="pending Runtime cleanup"):
        registry.create_business_agent(name="new", agent_id="agent-a", workspace_dir="/new/workspace")

    # 模拟 API 进程重启：新 service 只依赖持久化 intent，从首轮已确认进度继续。
    restarted = RuntimeAgentDeletionService(
        client=client,  # type: ignore[arg-type]
        store=RuntimeRunStore(store.Session),
        registry=AgentRegistryStore(store.Session),
        snapshots=PublishedHarnessSnapshotStore(snapshots.root),
        data_dir=tmp_path / "data",
        evict_agent_store=evicted.append,
    )
    restarted.registry.deletion_pending = restarted.store.agent_deletion_pending
    complete = asyncio.run(restarted.resume(intent.intent_id, assert_maintenance_active=lambda: None))

    assert complete.cleanup_complete is True
    completed = store.get_agent_deletion(intent.intent_id)
    assert completed.status == "cleanup_complete"
    assert completed.deleted_session_ids_json == ["session-a", "session-b"]
    assert completed.deleted_runtime_agent_ids_json == ["runtime-a"]
    assert completed.removed_snapshot_ids_json == [
        f"published::{retained_run.agent_version_id}:{retained_run.harness_digest}",
    ]
    assert not snapshot.workspace.parent.exists()
    assert not Path(record.workspace_dir).parent.exists()
    assert client.sessions["runtime-a"] == set()
    assert client.agents == set()
    assert evicted == ["agent-a"]
    with pytest.raises(RuntimeObjectNotFound):
        store.get_session("session-a")
    # run/trace/feedback 归属历史不级联；旧代际仍在，但不能再取得 Runtime 授权。
    assert store.get_run(retained_run.run_id).run_id == retained_run.run_id

    new_workspace = tmp_path / "data" / "business-agents" / "agent-a" / "workspace"
    create_test_business_agent_workspace(new_workspace, agent_id="agent-a", name="New Agent A")
    recreated = registry.create_business_agent(
        name="New Agent A",
        agent_id="agent-a",
        workspace_dir=str(new_workspace),
    )
    assert recreated.created_at > record.created_at
    provisioner = RuntimeAgentProvisioner(
        client=client,  # type: ignore[arg-type]
        store=store,
        registry=registry,
        version_store_for=lambda _agent_id: versions,
        snapshot_store=snapshots,
    )
    with pytest.raises(RuntimeObjectNotFound, match="active Agent generation"):
        provisioner.authorize_run(retained_run)


def test_remote_404_is_an_idempotent_cleanup_confirmation(tmp_path: Path) -> None:
    record, _versions, store, _registry, _snapshots, _snapshot, client, service, _run, _evicted = _fixture(tmp_path)
    intent = service.start(record)
    client.fail_session_once = None
    client.sessions["runtime-a"].clear()
    client.agents.clear()

    result = asyncio.run(service.resume(intent.intent_id, assert_maintenance_active=lambda: None))

    assert result.cleanup_complete is True
    completed = store.get_agent_deletion(intent.intent_id)
    assert completed.enumerated_runtime_agent_ids_json == ["runtime-a"]
    assert completed.deleted_session_ids_json == ["session-a", "session-b"]
    assert completed.deleted_runtime_agent_ids_json == ["runtime-a"]


def test_business_agent_deletion_waits_for_unlocated_ephemeral_provisioning(tmp_path: Path) -> None:
    record, _versions, store, registry, _snapshots, _snapshot, _client, service, retained_run, _evicted = _fixture(
        tmp_path,
    )
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

    with pytest.raises(RuntimeStateConflict, match="provisioning must settle"):
        service.start(record)

    assert registry.get_agent("agent-a") is not None
    assert store.recoverable_agent_deletions() == []


def test_business_agent_deletion_includes_candidate_ephemeral_source_without_touching_other_tuple(
    tmp_path: Path,
) -> None:
    record, versions, store, _registry, snapshots, published, client, service, retained_run, _evicted = _fixture(
        tmp_path,
    )
    isolation_key = "candidate:deletion-cross"
    candidate_identity = snapshots.candidate_identity(
        agent_id="agent-a",
        agent_version_id=retained_run.agent_version_id,
        expected_digest=retained_run.harness_digest,
        isolation_key=isolation_key,
    )
    workspace_id = f"{candidate_identity.workspace_id}--s-session-intent-00000000-0000-0000-0000-000000000001"
    store.start_ephemeral_resource(
        cache_key=isolation_key,
        business_agent_id="agent-a",
        version_owner_id=candidate_identity.source_id,
        agent_version_id=retained_run.agent_version_id,
        digest=retained_run.harness_digest,
        source_id=candidate_identity.source_id,
        source_kind="candidate_snapshot",
        workspace_id=workspace_id,
    )
    candidate = snapshots.materialize_candidate(
        version_store=versions,
        agent_id="agent-a",
        agent_version_id=retained_run.agent_version_id,
        expected_digest=retained_run.harness_digest,
        isolation_key=isolation_key,
    )
    store.record_ephemeral_agent(isolation_key, "runtime-candidate")
    store.bind_agent_version(
        agent_id=candidate.source_id,
        agent_version_id=retained_run.agent_version_id,
        digest=retained_run.harness_digest,
        runtime_agent_id="runtime-candidate",
        governance_agent_id="agent-a",
        source_kind="candidate_snapshot",
        source_id=candidate.source_id,
    )
    store.record_ephemeral_session(isolation_key, "session-candidate")
    store.bind_session(
        session_id="session-candidate",
        agent_id="agent-a",
        agent_version_id=retained_run.agent_version_id,
        runtime_agent_id="runtime-candidate",
        digest=retained_run.harness_digest,
    )
    store.mark_ephemeral_ready(isolation_key)
    client.agents.add("runtime-candidate")
    client.sessions["runtime-candidate"] = {"session-candidate"}
    client.fail_session_once = None

    intent = service.start(record)
    result = asyncio.run(service.resume(intent.intent_id, assert_maintenance_active=lambda: None))

    assert result.cleanup_complete is True
    completed = store.get_agent_deletion(intent.intent_id)
    assert completed.deleted_runtime_agent_ids_json == ["runtime-a", "runtime-candidate"]
    assert not published.workspace.parent.exists()
    assert not candidate.workspace.parent.exists()
    ephemeral = store.get_ephemeral_resource(isolation_key)
    assert ephemeral is not None and ephemeral.status == "cleanup_complete"
    assert store.agent_versions_for_agent("agent-a") == []
