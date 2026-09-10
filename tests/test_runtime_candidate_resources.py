from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime_gateway.client import RuntimeJsonResponse, RuntimeUpstreamError
from app.runtime_gateway.contracts import RuntimeChildSessionRegistration
from app.runtime_gateway.execution import AgentScopeExecutionService, _requires_interactive_continuation
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.store import RuntimeObjectNotFound, RuntimeRunStore, RuntimeStateConflict

from business_agent_test_utils import create_test_business_agent_workspace


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


class _Client:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.sessions: dict[str, set[str]] = {}
        self.agent_names: dict[str, str] = {}
        self.session_workspaces: dict[str, str] = {}
        self.fail_session_for_restart = False
        self.calls: list[tuple[str, str]] = []

    async def create_agent(self, payload: dict[str, object]) -> str:
        self.payloads.append(payload)
        runtime_agent_id = f"runtime-{len(self.payloads)}"
        self.sessions[runtime_agent_id] = set()
        self.agent_names[runtime_agent_id] = str(payload["name"])
        self.calls.append(("create_agent", runtime_agent_id))
        return runtime_agent_id

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        if method == "POST" and path == "/sessions/":
            if self.fail_session_for_restart:
                raise RuntimeUpstreamError(
                    500,
                    b'{"detail":"template published after Runtime startup; restart Runtime"}',
                )
            runtime_agent_id = kwargs["json"]["agent_id"]
            session_id = f"session-{len(self.sessions[runtime_agent_id]) + 1}"
            self.sessions[runtime_agent_id].add(session_id)
            self.session_workspaces[session_id] = kwargs["json"]["workspace_id"]
            self.calls.append(("create_session", session_id))
            return RuntimeJsonResponse(201, {}, {"session_id": session_id})
        if method == "PATCH" and path.startswith("/sessions/"):
            return RuntimeJsonResponse(200, {}, {})
        if method == "DELETE" and path.startswith("/sessions/"):
            session_id = path.rsplit("/", 1)[-1]
            runtime_agent_id = kwargs["params"]["agent_id"]
            await self.delete_session(session_id, runtime_agent_id)
            return RuntimeJsonResponse(204, {}, None)
        raise AssertionError((method, path, kwargs))

    async def list_session_ids(self, runtime_agent_id: str) -> list[str]:
        if runtime_agent_id not in self.sessions:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        return sorted(self.sessions[runtime_agent_id])

    async def list_session_ids_for_workspace(self, runtime_agent_id: str, workspace_id: str) -> list[str]:
        return [session_id for session_id in await self.list_session_ids(runtime_agent_id) if self.session_workspaces[session_id] == workspace_id]

    async def list_agent_ids_by_name(self, name: str) -> list[str]:
        return sorted(agent_id for agent_id, agent_name in self.agent_names.items() if agent_name == name)

    async def delete_session(self, session_id: str, runtime_agent_id: str) -> None:
        self.calls.append(("delete_session", session_id))
        sessions = self.sessions.get(runtime_agent_id)
        if sessions is None or session_id not in sessions:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        sessions.remove(session_id)
        self.session_workspaces.pop(session_id, None)

    async def delete_agent(self, runtime_agent_id: str) -> None:
        self.calls.append(("delete_agent", runtime_agent_id))
        if runtime_agent_id not in self.sessions:
            raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
        assert not self.sessions[runtime_agent_id]
        del self.sessions[runtime_agent_id]
        self.agent_names.pop(runtime_agent_id, None)


def _setup(tmp_path: Path):
    workspace = tmp_path / "agent" / "workspace"
    create_test_business_agent_workspace(workspace, agent_id="business-a", name="Candidate")
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.local")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "candidate")
    version_store = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    commit = version_store.current_commit_sha()
    assert commit is not None
    candidates = tmp_path / "candidates"
    settings = AppSettings(
        _env_file=None,
        RUNTIME_VOLUME_MODE="local-debug",
        DATA_DIR=tmp_path / "data",
        GOVERNOR_WORKSPACE_DIR=tmp_path / "governor",
        RUNTIME_CANDIDATES_DIR=candidates,
        AGENTGOV_RUNTIME_SHARED_SECRET="test-runtime-shared-secret",
    )
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    client = _Client()
    snapshots = PublishedHarnessSnapshotStore(candidates)
    service = AgentScopeExecutionService(
        settings=settings,
        client=client,  # type: ignore[arg-type]
        store=store,
        version_store_for=lambda _agent_id: version_store,
        snapshot_store=snapshots,
    )
    return workspace, version_store, commit, store, client, snapshots, service


def test_candidate_uses_exact_commit_and_terminal_release_gcs_all_ephemeral_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, version_store, commit, store, client, snapshots, service = _setup(tmp_path)
    original_materialize = snapshots.materialize_candidate
    ordering_evidence: list[str] = []

    def materialize_after_intent(**kwargs):
        ledger = store.get_ephemeral_resource("candidate:external")
        assert ledger is not None and ledger.status == "provisioning"
        ordering_evidence.append("intent_before_snapshot")
        return original_materialize(**kwargs)

    monkeypatch.setattr(snapshots, "materialize_candidate", materialize_after_intent)
    resource = asyncio.run(
        service._ensure_resource(
            cache_key="candidate:external",
            business_agent_id="business-a",
            version_id=commit,
            workspace=workspace,
            display_name="candidate",
            candidate_version_store=version_store,
        ),
    )

    assert ordering_evidence == ["intent_before_snapshot"]
    assert resource.version_id == commit
    assert resource.source_kind == "candidate_snapshot"
    assert resource.workspace_id.startswith("candidate-")
    assert "--s-session-intent-" in resource.workspace_id
    assert client.payloads[0]["system_prompt"] == "# Candidate\n\nBusiness Agent ID: `business-a`.\n"
    assert store.get_session(resource.session_id).agent_id == "business-a"
    assert (
        store.get_agent_version(
            agent_id=resource.version_owner_id,
            agent_version_id=commit,
            digest=resource.harness_digest,
        )
        is not None
    )
    assert (
        store.get_agent_version(
            agent_id="business-a",
            agent_version_id=commit,
            digest=resource.harness_digest,
        )
        is None
    )
    run = store.begin_run(
        session_id=resource.session_id,
        runtime_agent_id=resource.runtime_agent_id,
        input_value={"role": "user", "content": [{"type": "text", "text": "test"}]},
        alert_id=None,
        case_id=None,
        metadata={"tested_commit_sha": commit},
    )
    retained = store.fail_trigger(run.run_id, error={"type": "test"})
    source_root = resource.source_root

    asyncio.run(service.release_candidate("external"))

    assert retained.agent_id == "business-a"
    assert retained.agent_version_id == commit
    assert store.get_run(retained.run_id).metadata["tested_commit_sha"] == commit
    with pytest.raises(RuntimeObjectNotFound):
        store.get_session(resource.session_id)
    assert (
        store.get_agent_version(
            agent_id=resource.version_owner_id,
            agent_version_id=commit,
            digest=resource.harness_digest,
        )
        is None
    )
    assert not source_root.exists()
    assert client.sessions == {}
    assert client.calls.index(("delete_session", resource.session_id)) < client.calls.index(
        ("delete_agent", resource.runtime_agent_id),
    )


def test_noninteractive_execution_recognizes_projected_worker_hitl() -> None:
    projected = {
        "type": "CUSTOM",
        "name": "subagent_require_user_confirm",
        "value": {
            "worker_session_id": "worker-session",
            "event": {
                "type": "REQUIRE_USER_CONFIRM",
                "reply_id": "worker-reply",
            },
        },
    }
    assert _requires_interactive_continuation(projected) is True
    assert (
        _requires_interactive_continuation(
            {**projected, "name": "team_updated"},
        )
        is False
    )


def test_noninteractive_interrupt_stops_all_team_sessions_and_fences_partial_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, _versions, _commit, store, client, _snapshots, service = _setup(tmp_path)
    store.bind_agent_version(
        agent_id="business-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    store.bind_session(
        session_id="leader-session",
        agent_id="business-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="leader-session",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )
    calls: list[str] = []

    async def interrupt(method: str, path: str, **_kwargs: object) -> RuntimeJsonResponse:
        calls.append(path)
        if "worker-session" in path:
            raise RuntimeUpstreamError(503, b'{"detail":"provider-test-secret"}')
        return RuntimeJsonResponse(202, {}, {})

    monkeypatch.setattr(client, "request_json", interrupt)
    asyncio.run(service._interrupt_if_active("leader-session", "runtime-a"))

    assert calls == [
        "/sessions/leader-session/interrupt",
        "/sessions/worker-session/interrupt",
    ]
    active = store.get_run(run.run_id)
    assert active.metadata["cancellation_requested"] is True
    assert active.metadata["recovery_required"] is True
    assert active.error == {"type": "RuntimeUpstreamError"}
    assert "provider-test-secret" not in str(active.model_dump(mode="json"))


def test_candidate_live_worktree_drift_cannot_change_requested_commit_snapshot(tmp_path: Path) -> None:
    workspace, version_store, commit, _store, client, snapshots, service = _setup(tmp_path)
    (workspace / "AGENT.md").write_text("uncommitted drift\n", encoding="utf-8")

    with pytest.raises(Exception, match="Git commit Harness digest"):
        asyncio.run(
            service._ensure_resource(
                cache_key="candidate:drifted",
                business_agent_id="business-a",
                version_id=commit,
                workspace=workspace,
                display_name="candidate",
                candidate_version_store=version_store,
            ),
        )

    assert client.payloads == []
    assert list(snapshots.root.iterdir()) == []


def test_candidate_ephemeral_ledger_recovers_terminal_cleanup_after_process_restart(tmp_path: Path) -> None:
    workspace, version_store, commit, store, client, snapshots, first = _setup(tmp_path)
    resource = asyncio.run(
        first._ensure_resource(
            cache_key="candidate:restart",
            business_agent_id="business-a",
            version_id=commit,
            workspace=workspace,
            display_name="candidate",
            candidate_version_store=version_store,
        ),
    )
    assert store.get_ephemeral_resource("candidate:restart").status == "ready"  # type: ignore[union-attr]

    # 模拟进程重启：新的 service 没有 `_resources`，只依赖 SQLite ledger。
    restarted = AgentScopeExecutionService(
        settings=first.settings,
        client=client,  # type: ignore[arg-type]
        store=RuntimeRunStore(store.Session),
        version_store_for=lambda _agent_id: version_store,
        snapshot_store=PublishedHarnessSnapshotStore(snapshots.root),
    )
    asyncio.run(restarted.release_candidate("restart"))

    completed = store.get_ephemeral_resource("candidate:restart")
    assert completed is not None and completed.status == "cleanup_complete"
    assert client.calls.index(("delete_session", resource.session_id)) < client.calls.index(
        ("delete_agent", resource.runtime_agent_id),
    )
    assert client.sessions == {}
    assert not resource.source_root.exists()
    with pytest.raises(RuntimeObjectNotFound):
        store.get_session(resource.session_id)


def test_candidate_subagent_snapshot_waits_for_runtime_restart_and_same_key_resumes(tmp_path: Path) -> None:
    workspace, version_store, _commit, store, client, snapshots, first = _setup(tmp_path)
    subagent = workspace / "subagents" / "helper"
    subagent.mkdir(parents=True)
    (subagent / "agent.yaml").write_text("agent: {id: helper}\n", encoding="utf-8")
    (subagent / "AGENT.md").write_text("helper\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "candidate subagent")
    commit = version_store.inspect_clean_head()[0]
    client.fail_session_for_restart = True

    with pytest.raises(RuntimeStateConflict, match="restart AgentScope Runtime"):
        asyncio.run(
            first._ensure_resource(
                cache_key="candidate:subagent",
                business_agent_id="business-a",
                version_id=commit,
                workspace=workspace,
                display_name="candidate",
                candidate_version_store=version_store,
            ),
        )

    prepared = store.get_ephemeral_resource("candidate:subagent")
    assert prepared is not None and prepared.status == "awaiting_restart"
    source_root = snapshots.root / prepared.source_id
    assert source_root.is_dir()
    assert prepared.runtime_agent_id in client.sessions

    # 模拟 Runtime 已重启并装载 snapshot：新 API service 用同 cache_key 续建 Session。
    client.fail_session_for_restart = False
    restarted = AgentScopeExecutionService(
        settings=first.settings,
        client=client,  # type: ignore[arg-type]
        store=RuntimeRunStore(store.Session),
        version_store_for=lambda _agent_id: version_store,
        snapshot_store=PublishedHarnessSnapshotStore(snapshots.root),
    )
    resource = asyncio.run(
        restarted._ensure_resource(
            cache_key="candidate:subagent",
            business_agent_id="business-a",
            version_id=commit,
            workspace=workspace,
            display_name="candidate",
            candidate_version_store=version_store,
        ),
    )
    resumed = store.get_ephemeral_resource("candidate:subagent")
    assert resumed is not None and resumed.status == "ready"
    assert resource.runtime_agent_id == prepared.runtime_agent_id
    asyncio.run(restarted.release_candidate("subagent"))
    assert not source_root.exists()


def test_awaiting_restart_resource_is_cleaned_after_api_restart_or_ttl(tmp_path: Path) -> None:
    workspace, version_store, _commit, store, client, snapshots, first = _setup(tmp_path)
    subagent = workspace / "subagents" / "helper"
    subagent.mkdir(parents=True)
    (subagent / "agent.yaml").write_text("agent: {id: helper}\n", encoding="utf-8")
    (subagent / "AGENT.md").write_text("helper\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "candidate subagent for recovery")
    commit = version_store.inspect_clean_head()[0]
    client.fail_session_for_restart = True

    with pytest.raises(RuntimeStateConflict, match="restart AgentScope Runtime"):
        asyncio.run(
            first._ensure_resource(
                cache_key="candidate:restart-orphan",
                business_agent_id="business-a",
                version_id=commit,
                workspace=workspace,
                display_name="candidate",
                candidate_version_store=version_store,
            ),
        )
    assert store.get_ephemeral_resource("candidate:restart-orphan").status == "awaiting_restart"  # type: ignore[union-attr]
    assert (
        store.recoverable_ephemeral_resources(
            include_ready=False,
            awaiting_restart_before="2000-01-01T00:00:00+00:00",
        )
        == []
    )
    ttl_expired = store.recoverable_ephemeral_resources(
        include_ready=False,
        awaiting_restart_before="9999-01-01T00:00:00+00:00",
    )
    assert [row.cache_key for row in ttl_expired] == ["candidate:restart-orphan"]

    restarted = AgentScopeExecutionService(
        settings=first.settings,
        client=client,  # type: ignore[arg-type]
        store=RuntimeRunStore(store.Session),
        version_store_for=lambda _agent_id: version_store,
        snapshot_store=PublishedHarnessSnapshotStore(snapshots.root),
    )
    cleaned = asyncio.run(restarted.reconcile_ephemeral_resources(include_ready=True))

    assert cleaned == 1
    completed = store.get_ephemeral_resource("candidate:restart-orphan")
    assert completed is not None and completed.status == "cleanup_complete"
