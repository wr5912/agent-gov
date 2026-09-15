from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.runtime.agent_admission import (
    AgentMaintenanceActiveError,
    AgentMaintenanceClaimLost,
    AgentRunsActiveError,
    acquire_maintenance,
    assert_maintenance_claim_active,
    claim_runtime_admission,
    release_maintenance,
    renew_maintenance,
    run_maintenance_activation_guard,
)
from app.runtime.runtime_db import AgentAdmissionStateModel, AgentRunModel, make_session_factory
from app.runtime.runtime_db_base import utc_now
from app.runtime_gateway.store import RuntimeRunStore
from app.services.agent_version_maintenance import (
    AgentVersionMaintenanceCoordinator,
    is_agent_version_maintenance_active,
)


def _active_run(*, agent_id: str, run_id: str = "run-a", session_id: str = "session-a") -> AgentRunModel:
    return AgentRunModel(
        run_id=run_id,
        session_id=session_id,
        agent_id=agent_id,
        agent_version_id="version-a",
        runtime_agent_id=f"runtime-{agent_id}",
        harness_digest="a" * 64,
        status="running",
        reply_ids_json=[],
        trace_id=("1" if agent_id == "agent-a" else "2") * 32,
        trace_status="pending",
        metadata_json={},
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _bind_session(store: RuntimeRunStore, *, agent_id: str = "agent-a", session_id: str = "session-a") -> None:
    store.bind_session(
        session_id=session_id,
        agent_id=agent_id,
        agent_version_id="version-a",
        runtime_agent_id=f"runtime-{agent_id}",
        digest="a" * 64,
        idempotency_key=None,
    )


def test_durable_maintenance_blocks_runtime_for_only_its_agent(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    coordinator = AgentVersionMaintenanceCoordinator(factory, lease_seconds=2, heartbeat_seconds=0.1)

    with coordinator.lease(agent_id="agent-a", kind="publish", owner_id="test"):
        assert is_agent_version_maintenance_active(session_factory=factory, agent_id="agent-a")
        assert not is_agent_version_maintenance_active(session_factory=factory, agent_id="agent-b")
        with factory.begin() as db:
            with pytest.raises(AgentMaintenanceActiveError):
                claim_runtime_admission(db, agent_id="agent-a")
        with factory.begin() as db:
            assert claim_runtime_admission(db, agent_id="agent-b") > 0


def test_active_agentscope_run_blocks_maintenance_but_not_another_agent(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        db.add(_active_run(agent_id="agent-a"))

    with pytest.raises(AgentRunsActiveError):
        acquire_maintenance(
            factory,
            agent_id="agent-a",
            kind="restore",
            owner_id="test",
            lease_seconds=60,
        )
    other = acquire_maintenance(
        factory,
        agent_id="agent-b",
        kind="restore",
        owner_id="test",
        lease_seconds=60,
    )
    assert release_maintenance(factory, other)


def test_expired_maintenance_takeover_fences_stale_heartbeat_and_release(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    stale = acquire_maintenance(
        factory,
        agent_id="agent-a",
        kind="publish",
        owner_id="old",
        lease_seconds=1,
        now="2026-07-13T00:00:00+00:00",
    )
    replacement = acquire_maintenance(
        factory,
        agent_id="agent-a",
        kind="restore",
        owner_id="new",
        lease_seconds=60,
        now="2026-07-13T00:00:02+00:00",
    )

    assert replacement.generation > stale.generation
    with pytest.raises(AgentMaintenanceClaimLost):
        assert_maintenance_claim_active(factory, stale, now="2026-07-13T00:00:02+00:00")
    assert_maintenance_claim_active(factory, replacement, now="2026-07-13T00:00:03+00:00")
    with pytest.raises(AgentMaintenanceClaimLost):
        renew_maintenance(factory, stale, lease_seconds=60, now="2026-07-13T00:00:03+00:00")
    activated = False

    def activate(_db) -> None:
        nonlocal activated
        activated = True

    with pytest.raises(AgentMaintenanceClaimLost):
        run_maintenance_activation_guard(
            factory,
            stale,
            activate,
            lambda: None,
            now="2026-07-13T00:00:03+00:00",
        )
    assert not activated
    assert not release_maintenance(factory, stale)
    assert release_maintenance(factory, replacement)


@pytest.mark.parametrize("maintenance_kind", ["workspace_import", "workspace_restore"])
def test_expired_workspace_maintenance_admits_a_version_pinned_run(
    tmp_path,
    maintenance_kind: str,
) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = RuntimeRunStore(factory)
    _bind_session(store)
    acquire_maintenance(
        factory,
        agent_id="agent-a",
        kind=maintenance_kind,
        owner_id="crashed-worker",
        lease_seconds=1,
        now="2026-07-13T00:00:00+00:00",
    )

    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-agent-a",
        input_value={"type": "text", "text": "continue safely"},
        entities={},
        metadata={},
    )

    assert run.status.value == "queued"
    assert run.agent_version_id == "version-a"
    assert run.harness_digest == "a" * 64
    with factory() as db:
        state = db.get(AgentAdmissionStateModel, "agent-a")
        assert state is not None and state.maintenance_token is None


def test_restart_reconcile_keeps_runtime_fence_and_blocks_maintenance(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = RuntimeRunStore(factory)
    _bind_session(store)
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-agent-a",
        input_value="work",
        entities={},
        metadata={},
    )

    with pytest.raises(AgentRunsActiveError):
        acquire_maintenance(factory, agent_id="agent-a", kind="publish", owner_id="test", lease_seconds=60)
    assert store.reconcile_after_restart() == [run.run_id]
    assert store.get_run(run.run_id).status.value == "queued"
    with pytest.raises(AgentRunsActiveError):
        acquire_maintenance(factory, agent_id="agent-a", kind="publish", owner_id="test", lease_seconds=60)


def test_sqlite_write_barrier_serializes_runtime_claim_before_maintenance(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    runtime_claimed = threading.Event()
    maintenance_started = threading.Event()
    commit_runtime = threading.Event()

    def claim_runtime_and_hold_transaction() -> None:
        with factory.begin() as db:
            claim_runtime_admission(db, agent_id="agent-a")
            db.add(_active_run(agent_id="agent-a"))
            runtime_claimed.set()
            assert commit_runtime.wait(timeout=5)

    def claim_maintenance_after_runtime_write() -> None:
        assert runtime_claimed.wait(timeout=5)
        maintenance_started.set()
        acquire_maintenance(
            factory,
            agent_id="agent-a",
            kind="publish",
            owner_id="test",
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        runtime_future = executor.submit(claim_runtime_and_hold_transaction)
        maintenance_future = executor.submit(claim_maintenance_after_runtime_write)
        assert runtime_claimed.wait(timeout=5)
        assert maintenance_started.wait(timeout=5)
        assert not maintenance_future.done()
        commit_runtime.set()
        runtime_future.result(timeout=5)
        with pytest.raises(AgentRunsActiveError):
            maintenance_future.result(timeout=5)


def test_activation_guard_serializes_runtime_admission_until_side_effect_finishes(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    claim = acquire_maintenance(
        factory,
        agent_id="agent-a",
        kind="workspace_import",
        owner_id="test",
        lease_seconds=60,
    )
    activation_started = threading.Event()
    finish_activation = threading.Event()
    runtime_started = threading.Event()
    runtime_finished = threading.Event()

    def activate_and_hold_write_barrier(_db) -> None:
        activation_started.set()
        assert finish_activation.wait(timeout=5)

    def claim_runtime_while_activation_is_held() -> int:
        assert activation_started.wait(timeout=5)
        runtime_started.set()
        try:
            with factory.begin() as db:
                return claim_runtime_admission(db, agent_id="agent-a")
        finally:
            runtime_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        activation_future = executor.submit(
            run_maintenance_activation_guard,
            factory,
            claim,
            activate_and_hold_write_barrier,
            lambda: None,
        )
        assert activation_started.wait(timeout=5)
        runtime_future = executor.submit(claim_runtime_while_activation_is_held)
        assert runtime_started.wait(timeout=5)
        assert not runtime_finished.wait(timeout=0.2)
        finish_activation.set()
        activation_future.result(timeout=5)
        with pytest.raises(AgentMaintenanceActiveError):
            runtime_future.result(timeout=5)

    assert release_maintenance(factory, claim)
