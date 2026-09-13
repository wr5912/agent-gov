from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime_gateway._execution_support import _requires_interactive_continuation
from app.runtime_gateway.client import RuntimeJsonResponse, RuntimeUpstreamError
from app.runtime_gateway.contracts import RuntimeChildSessionRegistration
from app.runtime_gateway.execution import AgentScopeExecutionService
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.run_trigger import cancel_active_session_run
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict, harness_digest

from business_agent_test_utils import create_test_business_agent_workspace


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(tmp_path: Path) -> tuple[Path, GitAgentVersionStore, str, str]:
    workspace = tmp_path / "agent" / "workspace"
    create_test_business_agent_workspace(workspace, agent_id="business-a", name="Candidate")
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.local")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "candidate")
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    commit = versions.inspect_clean_head()[0]
    return workspace, versions, commit, harness_digest(workspace)


def _start_resource(store: RuntimeRunStore) -> None:
    store.start_ephemeral_resource(
        cache_key="candidate:resource",
        business_agent_id="business-a",
        version_owner_id=f"candidate-{'a' * 48}",
        agent_version_id="a" * 40,
        digest="b" * 64,
        source_id=f"candidate-{'a' * 48}",
        source_kind="candidate_snapshot",
        workspace_id=f"candidate-{'a' * 48}--v-{'b' * 64}",
    )


def test_candidate_snapshot_uses_exact_commit_despite_live_workspace_drift(tmp_path: Path) -> None:
    workspace, versions, commit, digest = _workspace(tmp_path)
    (workspace / "AGENT.md").write_text("uncommitted drift\n", encoding="utf-8")
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "candidates")

    candidate = snapshots.materialize_candidate(
        version_store=versions,
        agent_id="business-a",
        agent_version_id=commit,
        expected_digest=digest,
        isolation_key="candidate:exact-commit",
    )

    assert candidate.agent_version_id == commit
    assert candidate.source_id.startswith("candidate-")
    assert candidate.workspace.joinpath("AGENT.md").read_text(encoding="utf-8") != "uncommitted drift\n"
    assert workspace.joinpath("AGENT.md").read_text(encoding="utf-8") == "uncommitted drift\n"


def test_candidate_snapshot_rejects_live_digest_for_an_older_commit(tmp_path: Path) -> None:
    workspace, versions, commit, _digest = _workspace(tmp_path)
    (workspace / "AGENT.md").write_text("uncommitted drift\n", encoding="utf-8")
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "candidates")

    with pytest.raises(RuntimeStateConflict, match="digest does not match"):
        snapshots.materialize_candidate(
            version_store=versions,
            agent_id="business-a",
            agent_version_id=commit,
            expected_digest=harness_digest(workspace),
            isolation_key="candidate:drifted",
        )

    assert list(snapshots.root.iterdir()) == []


def test_noninteractive_execution_recognizes_projected_worker_hitl() -> None:
    projected = {
        "type": "CUSTOM",
        "name": "subagent_require_user_confirm",
        "value": {
            "worker_session_id": "worker-session",
            "event": {"type": "REQUIRE_USER_CONFIRM", "reply_id": "worker-reply"},
        },
    }

    assert _requires_interactive_continuation(projected) is True
    assert _requires_interactive_continuation({**projected, "name": "team_updated"}) is False


def test_ephemeral_ledger_is_idempotent_and_rejects_cache_key_rebinding(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    _start_resource(store)
    first = store.get_ephemeral_resource("candidate:resource")
    _start_resource(store)
    repeated = store.get_ephemeral_resource("candidate:resource")

    assert first is not None and repeated is not None
    assert repeated.created_at == first.created_at
    assert repeated.status == "provisioning"
    with pytest.raises(RuntimeStateConflict, match="another immutable resource"):
        store.start_ephemeral_resource(
            cache_key="candidate:resource",
            business_agent_id="business-a",
            version_owner_id=f"candidate-{'c' * 48}",
            agent_version_id="c" * 40,
            digest="d" * 64,
            source_id=f"candidate-{'c' * 48}",
            source_kind="candidate_snapshot",
            workspace_id=f"candidate-{'c' * 48}--v-{'d' * 64}",
        )


def test_ephemeral_ledger_requires_exact_agent_and_session_before_ready(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    _start_resource(store)

    with pytest.raises(RuntimeStateConflict, match="without Agent and Session"):
        store.mark_ephemeral_ready("candidate:resource")
    store.record_ephemeral_agent("candidate:resource", "runtime-candidate")
    with pytest.raises(RuntimeStateConflict, match="without Agent and Session"):
        store.mark_ephemeral_ready("candidate:resource")
    store.record_ephemeral_session("candidate:resource", "session-candidate")
    ready = store.mark_ephemeral_ready("candidate:resource")

    assert ready.status == "ready"
    with pytest.raises(RuntimeStateConflict, match="another Runtime Agent"):
        store.record_ephemeral_agent("candidate:resource", "runtime-other")
    with pytest.raises(RuntimeStateConflict, match="another Runtime Session"):
        store.record_ephemeral_session("candidate:resource", "session-other")


def test_ephemeral_cleanup_waits_for_real_sqlite_bindings(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    _start_resource(store)
    store.record_ephemeral_agent("candidate:resource", "runtime-candidate")
    store.record_ephemeral_session("candidate:resource", "session-candidate")
    store.bind_agent_version(
        agent_id=f"candidate-{'a' * 48}",
        governance_agent_id="business-a",
        agent_version_id="a" * 40,
        digest="b" * 64,
        runtime_agent_id="runtime-candidate",
        source_kind="candidate_snapshot",
        source_id=f"candidate-{'a' * 48}",
    )
    store.bind_session(
        session_id="session-candidate",
        agent_id="business-a",
        agent_version_id="a" * 40,
        runtime_agent_id="runtime-candidate",
        digest="b" * 64,
    )
    store.mark_ephemeral_ready("candidate:resource")

    with pytest.raises(RuntimeStateConflict, match="bindings still exist"):
        store.complete_ephemeral_resource("candidate:resource")
    assert store.delete_session_binding("session-candidate") is True
    assert store.delete_agent_version(
        agent_id=f"candidate-{'a' * 48}",
        agent_version_id="a" * 40,
        digest="b" * 64,
    )
    completed = store.complete_ephemeral_resource("candidate:resource")
    assert completed.status == "cleanup_complete"


def test_awaiting_restart_ledger_is_recoverable_only_after_the_requested_cutoff(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    _start_resource(store)
    awaiting = store.mark_ephemeral_awaiting_restart(
        "candidate:resource",
        stage="create_session",
        error_type="RuntimeUpstreamError",
    )

    assert (
        store.recoverable_ephemeral_resources(
            include_ready=False,
            awaiting_restart_before="2000-01-01T00:00:00+00:00",
        )
        == []
    )
    recovered = store.recoverable_ephemeral_resources(
        include_ready=False,
        awaiting_restart_before="9999-01-01T00:00:00+00:00",
    )
    assert [row.cache_key for row in recovered] == [awaiting.cache_key]


class _PartialInterruptClient:
    """稳定制造 Team 子 Session interrupt 部分失败。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request_json(self, method: str, path: str, **_kwargs: object) -> RuntimeJsonResponse:
        assert method == "POST"
        self.calls.append(path)
        if "worker-session" in path:
            raise RuntimeUpstreamError(503, b'{"detail":"provider-test-secret"}')
        return RuntimeJsonResponse(202, {}, {})


def test_candidate_interrupt_partial_failure_keeps_all_team_fences_and_redacts(
    tmp_path: Path,
) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "partial-interrupt.db"))
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
    runtime = _PartialInterruptClient()
    settings = AppSettings(
        _env_file=None,
        RUNTIME_VOLUME_MODE="local-debug",
        DATA_DIR=tmp_path / "data",
        GOVERNOR_WORKSPACE_DIR=tmp_path / "governor",
        RUNTIME_CANDIDATES_DIR=tmp_path / "candidates",
        AGENTGOV_RUNTIME_SHARED_SECRET="test-runtime-shared-secret",
    )
    service = AgentScopeExecutionService(
        settings=settings,
        client=runtime,  # type: ignore[arg-type]
        store=store,
        version_store_for=lambda _agent_id: (_ for _ in ()).throw(AssertionError("unused")),
        snapshot_store=PublishedHarnessSnapshotStore(tmp_path / "candidates"),
    )

    asyncio.run(
        cancel_active_session_run(
            client=service.client,
            store=service.store,
            session_id="leader-session",
        ),
    )

    assert runtime.calls == [
        "/sessions/leader-session/interrupt",
        "/sessions/worker-session/interrupt",
    ]
    active = store.get_run(run.run_id)
    assert active.metadata["cancellation_requested"] is True
    assert active.metadata["recovery_required"] is True
    assert active.error == {"type": "RuntimeUpstreamError"}
    assert "provider-test-secret" not in str(active.model_dump(mode="json"))
    assert store.get_session("leader-session").active_run_id == run.run_id
    assert store.get_session("worker-session").active_run_id == run.run_id
