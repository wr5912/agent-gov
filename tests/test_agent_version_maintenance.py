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
from app.runtime.runtime_db import (
    AgentAdmissionStateModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    make_session_factory,
)
from app.runtime.session_store import LocalSession, LocalSessionStore
from app.services.agent_version_maintenance import (
    AgentVersionMaintenanceCoordinator,
    is_agent_version_maintenance_active,
)


def test_durable_maintenance_blocks_runtime_for_only_its_agent(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    coordinator = AgentVersionMaintenanceCoordinator(
        factory,
        lease_seconds=2,
        heartbeat_seconds=0.1,
    )

    with coordinator.lease(agent_id="agent-a", kind="publish", owner_id="test"):
        assert is_agent_version_maintenance_active(
            session_factory=factory,
            agent_id="agent-a",
        )
        assert not is_agent_version_maintenance_active(
            session_factory=factory,
            agent_id="agent-b",
        )
        with factory.begin() as db:
            with pytest.raises(AgentMaintenanceActiveError):
                claim_runtime_admission(db, agent_id="agent-a")
        with factory.begin() as db:
            assert claim_runtime_admission(db, agent_id="agent-b") > 0


def test_active_runtime_turn_blocks_maintenance_but_not_another_agent(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        db.add(
            SessionRecordModel(
                session_id="session-a",
                agent_id="agent-a",
                active_run_id="run-a",
                active_run_generation=1,
                active_run_expires_at="2099-01-01T00:00:00+00:00",
                metadata_json={},
            )
        )

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
        assert_maintenance_claim_active(
            factory,
            stale,
            now="2026-07-13T00:00:02+00:00",
        )
    assert_maintenance_claim_active(
        factory,
        replacement,
        now="2026-07-13T00:00:03+00:00",
    )
    with pytest.raises(AgentMaintenanceClaimLost):
        renew_maintenance(
            factory,
            stale,
            lease_seconds=60,
            now="2026-07-13T00:00:03+00:00",
        )
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
def test_expired_workspace_activation_claim_invalidates_stale_sdk_mapping(
    tmp_path,
    maintenance_kind: str,
) -> None:
    store = LocalSessionStore(tmp_path / "sessions")
    store.save(
        LocalSession(
            session_id="session-a",
            agent_id="agent-a",
            sdk_session_id="stale-sdk-session",
            turns=1,
        )
    )
    acquire_maintenance(
        store.Session,
        agent_id="agent-a",
        kind=maintenance_kind,
        owner_id="crashed-worker",
        lease_seconds=1,
        now="2026-07-13T00:00:00+00:00",
    )
    stale_snapshot = store.get("session-a")
    assert stale_snapshot is not None

    admission = store.begin_persisted_turn(
        stale_snapshot,
        run_id="run-after-crash",
        agent_id="agent-a",
        new_sdk_session_id="fresh-sdk-session",
        sdk_project_key="project-a",
        resolve_agent_version_id=lambda: "version-after-crash",
        request={"message": "resume safely"},
        created_at="2026-07-13T00:00:02+00:00",
    )

    with store.Session() as db:
        state = db.get(AgentAdmissionStateModel, "agent-a")
        intent = db.get(SessionTurnIntentModel, "run-after-crash")
        assert state is not None
        assert intent is not None
        assert state.maintenance_token is None
        assert intent.source_sdk_session_id is None
        assert intent.attempted_sdk_session_id == "fresh-sdk-session"
    assert admission.session.sdk_session_id is None
    assert admission.attempted_sdk_session_id == "fresh-sdk-session"
    assert admission.agent_version_id == "version-after-crash"


def test_agent_version_resolver_failure_rolls_back_runtime_admission(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path / "sessions")
    session = store.get_or_create_owned("session-a", agent_id="agent-a")

    def fail_version_resolution() -> str:
        raise RuntimeError("injected version resolution failure")

    with pytest.raises(RuntimeError, match="injected version resolution failure"):
        store.begin_persisted_turn(
            session,
            run_id="run-a",
            agent_id="agent-a",
            new_sdk_session_id="new-sdk",
            sdk_project_key="project-a",
            resolve_agent_version_id=fail_version_resolution,
            request={},
            created_at="2026-07-13T00:00:00+00:00",
        )

    saved = store.get("session-a")
    with store.Session() as db:
        assert db.get(SessionTurnIntentModel, "run-a") is None
    assert saved is not None
    assert saved.active_run_id is None
    claim = acquire_maintenance(
        store.Session,
        agent_id="agent-a",
        kind="workspace_export",
        owner_id="test",
        lease_seconds=60,
    )
    assert release_maintenance(store.Session, claim)


def test_expired_runtime_with_running_intent_fails_closed_until_reconciled(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        db.add(
            SessionRecordModel(
                session_id="session-a",
                agent_id="agent-a",
                active_run_id="run-a",
                active_run_generation=1,
                active_run_expires_at="2026-07-13T00:00:00+00:00",
                metadata_json={},
            )
        )
        db.add(
            SessionTurnIntentModel(
                run_id="run-a",
                session_id="session-a",
                agent_id="agent-a",
                attempted_sdk_session_id="sdk-a",
                sdk_project_key="project-a",
                base_turns=0,
                status="running",
                request_json={},
                error_json={},
            )
        )

    with pytest.raises(AgentRunsActiveError):
        acquire_maintenance(
            factory,
            agent_id="agent-a",
            kind="publish",
            owner_id="test",
            lease_seconds=60,
            now="2026-07-13T00:00:01+00:00",
        )

    with factory.begin() as db:
        db.get(SessionTurnIntentModel, "run-a").status = "interrupted"
    claim = acquire_maintenance(
        factory,
        agent_id="agent-a",
        kind="publish",
        owner_id="test",
        lease_seconds=60,
        now="2026-07-13T00:00:02+00:00",
    )
    with factory() as db:
        session = db.get(SessionRecordModel, "session-a")
        assert session is not None and session.active_run_id is None
    assert release_maintenance(factory, claim)


def test_sqlite_write_barrier_serializes_runtime_claim_before_maintenance(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        db.add(SessionRecordModel(session_id="session-a", agent_id="agent-a", metadata_json={}))

    runtime_claimed = threading.Event()
    maintenance_started = threading.Event()
    commit_runtime = threading.Event()

    def claim_runtime_and_hold_transaction() -> None:
        with factory.begin() as db:
            generation = claim_runtime_admission(db, agent_id="agent-a")
            session = db.get(SessionRecordModel, "session-a")
            assert session is not None
            session.active_run_id = "run-a"
            session.active_run_generation = generation
            session.active_run_expires_at = "2099-01-01T00:00:00+00:00"
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
