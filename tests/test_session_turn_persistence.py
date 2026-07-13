import asyncio

import pytest
from app.runtime.agent_admission import AgentRunsActiveError, acquire_maintenance, release_maintenance
from app.runtime.errors import SessionConflictError
from app.runtime.records.source_records import AgentRunRecord
from app.runtime.runtime_db import (
    AgentRunModel,
    SdkSessionEntryModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    make_session_factory,
)
from app.runtime.sdk_session_store import SqliteSdkSessionStore
from app.runtime.session_store import LocalSessionStore
from app.runtime.session_turn_persistence import (
    abort_persisted_turn,
    complete_persisted_turn,
    reconcile_expired_turns,
)


def _seed_turn(tmp_path, *, expires_at="2999-01-01T00:00:00+00:00", append_entry=True):
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        db.add(
            SessionRecordModel(
                session_id="api-session",
                sdk_session_id=None,
                agent_id="main-agent",
                created_at="2026-07-13T00:00:00+00:00",
                updated_at="2026-07-13T00:00:00+00:00",
                turns=0,
                metadata_json={},
                active_run_id="run-1",
                active_run_expires_at="2999-01-01T00:00:00+00:00",
                active_run_generation=7,
            )
        )
        db.add(
            SessionTurnIntentModel(
                run_id="run-1",
                session_id="api-session",
                agent_id="main-agent",
                source_sdk_session_id=None,
                attempted_sdk_session_id="sdk-session",
                sdk_project_key="project-key",
                base_turns=0,
                status="running",
                request_json={"agent_version_id": "version-1", "alert_id": "alert-1"},
                error_json={},
                created_at="2026-07-13T00:00:00+00:00",
                updated_at="2026-07-13T00:00:00+00:00",
            )
        )
    store = SqliteSdkSessionStore.for_turn(
        factory,
        project_key="project-key",
        sdk_session_id="sdk-session",
        run_id="run-1",
    )
    if append_entry:
        asyncio.run(
            store.append(
                {"project_key": "project-key", "session_id": "sdk-session"},
                [{"type": "assistant", "uuid": "entry-1", "message": {"content": "answer"}}],
            )
        )
    if expires_at != "2999-01-01T00:00:00+00:00":
        with factory.begin() as db:
            session = db.get(SessionRecordModel, "api-session")
            assert session is not None
            session.active_run_expires_at = expires_at
    return factory


def _run_record(*, errors=None):
    return AgentRunRecord.from_payload(
        {
            "run_id": "run-1",
            "session_id": "api-session",
            "sdk_session_id": "sdk-session",
            "agent_version_id": "version-1",
            "created_at": "2026-07-13T00:00:00+00:00",
            "completed_at": "2026-07-13T00:01:00+00:00",
            "errors": errors or [],
        }
    )


def test_complete_publishes_transcript_session_run_and_intent_atomically(tmp_path):
    factory = _seed_turn(tmp_path)
    with factory.begin() as db:
        session = db.get(SessionRecordModel, "api-session")
        assert session is not None
        complete_persisted_turn(
            db,
            session=session,
            run_id="run-1",
            run_generation=7,
            sdk_session_id="sdk-session",
            title="hello",
            run_record=_run_record(),
            terminal_status="succeeded",
            completed_at="2026-07-13T00:01:00+00:00",
        )

    with factory() as db:
        session = db.get(SessionRecordModel, "api-session")
        intent = db.get(SessionTurnIntentModel, "run-1")
        entry = db.query(SdkSessionEntryModel).one()
        assert session is not None and intent is not None
        assert session.sdk_session_id == "sdk-session"
        assert session.sdk_project_key == "project-key"
        assert session.turns == 1
        assert session.active_run_id is None
        assert intent.status == "succeeded"
        assert entry.committed_at == "2026-07-13T00:01:00+00:00"
        assert db.get(AgentRunModel, "run-1") is not None


def test_complete_rejects_result_without_staged_sdk_transcript(tmp_path):
    factory = _seed_turn(tmp_path, append_entry=False)

    with pytest.raises(SessionConflictError, match="no staged transcript"):
        with factory.begin() as db:
            session = db.get(SessionRecordModel, "api-session")
            assert session is not None
            complete_persisted_turn(
                db,
                session=session,
                run_id="run-1",
                run_generation=7,
                sdk_session_id="sdk-session",
                title="empty",
                run_record=_run_record(),
                terminal_status="succeeded",
            )

    with factory() as db:
        session = db.get(SessionRecordModel, "api-session")
        intent = db.get(SessionTurnIntentModel, "run-1")
        assert session is not None and intent is not None
        assert session.turns == 0 and session.sdk_session_id is None
        assert session.active_run_id == "run-1"
        assert intent.status == "running"
        assert db.get(AgentRunModel, "run-1") is None


def test_finalize_exception_rolls_back_every_projection(tmp_path, monkeypatch):
    factory = _seed_turn(tmp_path)

    def fail_run_projection(*_args, **_kwargs):
        raise RuntimeError("injected finalization failure")

    monkeypatch.setattr(
        "app.runtime.session_turn_persistence.upsert_agent_run_record",
        fail_run_projection,
    )
    with pytest.raises(RuntimeError, match="injected"):
        with factory.begin() as db:
            session = db.get(SessionRecordModel, "api-session")
            assert session is not None
            complete_persisted_turn(
                db,
                session=session,
                run_id="run-1",
                run_generation=7,
                sdk_session_id="sdk-session",
                title="hello",
                run_record=_run_record(),
                terminal_status="succeeded",
            )

    with factory() as db:
        session = db.get(SessionRecordModel, "api-session")
        intent = db.get(SessionTurnIntentModel, "run-1")
        entry = db.query(SdkSessionEntryModel).one()
        assert session is not None and intent is not None
        assert session.turns == 0 and session.sdk_session_id is None
        assert session.active_run_id == "run-1"
        assert intent.status == "running"
        assert entry.committed_at is None and entry.discarded_at is None
        assert db.get(AgentRunModel, "run-1") is None


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_abort_discards_stage_without_advancing_session(tmp_path, terminal):
    factory = _seed_turn(tmp_path)
    with factory.begin() as db:
        session = db.get(SessionRecordModel, "api-session")
        assert session is not None
        abort_persisted_turn(
            db,
            session=session,
            run_id="run-1",
            run_generation=7,
            run_record=_run_record(errors=[terminal]),
            terminal_status=terminal,
            error={"type": terminal, "message": terminal},
            completed_at="2026-07-13T00:01:00+00:00",
        )

    with factory() as db:
        session = db.get(SessionRecordModel, "api-session")
        intent = db.get(SessionTurnIntentModel, "run-1")
        entry = db.query(SdkSessionEntryModel).one()
        assert session is not None and intent is not None
        assert session.turns == 0 and session.sdk_session_id is None
        assert session.active_run_id is None
        assert intent.status == terminal
        assert entry.committed_at is None
        assert entry.discarded_at == "2026-07-13T00:01:00+00:00"
        assert db.get(AgentRunModel, "run-1") is not None


def test_expired_reconcile_is_idempotent_and_fences_late_completion(tmp_path):
    factory = _seed_turn(tmp_path, expires_at="2026-07-13T00:00:30+00:00")

    assert reconcile_expired_turns(factory, now="2026-07-13T00:01:00+00:00") == ["run-1"]
    assert reconcile_expired_turns(factory, now="2026-07-13T00:02:00+00:00") == []

    with factory() as db:
        session = db.get(SessionRecordModel, "api-session")
        intent = db.get(SessionTurnIntentModel, "run-1")
        run = db.get(AgentRunModel, "run-1")
        entry = db.query(SdkSessionEntryModel).one()
        assert session is not None and intent is not None and run is not None
        assert session.turns == 0 and session.active_run_id is None
        assert intent.status == "interrupted"
        assert run.payload_json["turn_status"] == "interrupted"
        assert entry.discarded_at == "2026-07-13T00:01:00+00:00"

    with pytest.raises(SessionConflictError, match="already finalized"):
        with factory.begin() as db:
            session = db.get(SessionRecordModel, "api-session")
            assert session is not None
            complete_persisted_turn(
                db,
                session=session,
                run_id="run-1",
                run_generation=7,
                sdk_session_id="sdk-session",
                title="late",
                run_record=_run_record(),
                terminal_status="succeeded",
            )


def test_exact_completion_retry_allows_session_to_advance_to_next_turn(tmp_path):
    _seed_turn(tmp_path)
    store = LocalSessionStore(tmp_path / "sessions")
    completed_at = "2026-07-13T00:01:00+00:00"
    kwargs = {
        "session_id": "api-session",
        "run_id": "run-1",
        "run_generation": 7,
        "sdk_session_id": "sdk-session",
        "title": "hello",
        "run_record": _run_record(),
        "terminal_status": "succeeded",
        "completed_at": completed_at,
    }
    store.finalize_persisted_turn(**kwargs)
    current = store.get("api-session")
    assert current is not None
    next_turn = store.begin_persisted_turn(
        current,
        run_id="run-2",
        agent_id="main-agent",
        attempted_sdk_session_id="sdk-session",
        sdk_project_key="project-key",
        request={"agent_version_id": "version-2"},
        created_at="2026-07-13T00:02:00+00:00",
    )

    retried = store.finalize_persisted_turn(**kwargs)

    assert retried.turns == 1
    assert retried.active_run_id == "run-2"
    assert retried.active_run_generation == next_turn.active_run_generation


def test_exact_completion_retry_canonicalizes_optional_run_fields(tmp_path):
    _seed_turn(tmp_path)
    store = LocalSessionStore(tmp_path / "sessions")
    minimal_run = AgentRunRecord(
        run_id="run-1",
        session_id="api-session",
        sdk_session_id="sdk-session",
        agent_version_id="version-1",
        created_at="2026-07-13T00:00:00+00:00",
        completed_at="2026-07-13T00:01:00+00:00",
    )
    kwargs = {
        "session_id": "api-session",
        "run_id": "run-1",
        "run_generation": 7,
        "sdk_session_id": "sdk-session",
        "title": "hello",
        "run_record": minimal_run,
        "terminal_status": "succeeded",
        "completed_at": "2026-07-13T00:01:00+00:00",
    }

    store.finalize_persisted_turn(**kwargs)
    retried = store.finalize_persisted_turn(**kwargs)

    assert retried.turns == 1
    assert retried.active_run_id is None


def test_exact_abort_retry_allows_session_to_advance_to_next_turn(tmp_path):
    _seed_turn(tmp_path)
    store = LocalSessionStore(tmp_path / "sessions")
    completed_at = "2026-07-13T00:01:00+00:00"
    error = {"type": "cancelled", "message": "cancelled"}
    kwargs = {
        "session_id": "api-session",
        "run_id": "run-1",
        "run_generation": 7,
        "run_record": _run_record(errors=["cancelled"]),
        "terminal_status": "cancelled",
        "error": error,
        "completed_at": completed_at,
    }
    store.abort_persisted_turn(**kwargs)
    current = store.get("api-session")
    assert current is not None
    next_turn = store.begin_persisted_turn(
        current,
        run_id="run-2",
        agent_id="main-agent",
        attempted_sdk_session_id="sdk-next",
        sdk_project_key="project-key",
        request={"agent_version_id": "version-2"},
        created_at="2026-07-13T00:02:00+00:00",
    )

    retried = store.abort_persisted_turn(**kwargs)

    assert retried.turns == 0
    assert retried.active_run_id == "run-2"
    assert retried.active_run_generation == next_turn.active_run_generation


def test_expired_running_intent_reconciliation_unblocks_agent_maintenance(tmp_path):
    factory = _seed_turn(tmp_path, expires_at="2026-07-13T00:00:30+00:00")
    with pytest.raises(AgentRunsActiveError):
        acquire_maintenance(
            factory,
            agent_id="main-agent",
            kind="publish",
            owner_id="tester",
            lease_seconds=60,
            now="2999-01-01T00:00:00+00:00",
        )

    assert reconcile_expired_turns(factory, now="2999-01-01T00:00:00+00:00") == ["run-1"]
    claim = acquire_maintenance(
        factory,
        agent_id="main-agent",
        kind="publish",
        owner_id="tester",
        lease_seconds=60,
        now="2999-01-01T00:00:01+00:00",
    )
    assert release_maintenance(factory, claim)
