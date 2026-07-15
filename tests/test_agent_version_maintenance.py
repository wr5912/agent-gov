from __future__ import annotations

import threading
import time
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
)
from app.runtime.runtime_db import (
    SessionRecordModel,
    SessionTurnIntentModel,
    make_session_factory,
)
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
    assert not release_maintenance(factory, stale)
    assert release_maintenance(factory, replacement)


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
        time.sleep(0.1)
        assert not maintenance_future.done()
        commit_runtime.set()
        runtime_future.result(timeout=5)
        with pytest.raises(AgentRunsActiveError):
            maintenance_future.result(timeout=5)
