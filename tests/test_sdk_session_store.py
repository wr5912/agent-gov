import asyncio
import sqlite3

import pytest
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.errors import SessionConflictError
from app.runtime.runtime_db import (
    SdkSessionEntryModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    make_session_factory,
)
from app.runtime.sdk_session_store import (
    SqliteSdkSessionStore,
    discard_staged_entries,
    promote_staged_entries,
    reconcile_orphaned_staged_entries,
)
from app.runtime.session_store import LocalSessionStore
from sqlalchemy import event

from business_agent_test_utils import ORDINARY_TEST_AGENT_ID, register_test_business_agent_instance


def _factory_with_active_turn(tmp_path, *, run_id="run-1"):
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        db.add(
            SessionRecordModel(
                session_id="api-session",
                agent_id=ORDINARY_TEST_AGENT_ID,
                created_at="2026-07-13T00:00:00+00:00",
                updated_at="2026-07-13T00:00:00+00:00",
                turns=0,
                metadata_json={},
                active_run_id=run_id,
                active_run_expires_at="2999-01-01T00:00:00+00:00",
            )
        )
        db.add(
            SessionTurnIntentModel(
                run_id=run_id,
                session_id="api-session",
                agent_id=ORDINARY_TEST_AGENT_ID,
                attempted_sdk_session_id="sdk-session",
                sdk_project_key="project-key",
                base_turns=0,
                status="running",
                request_json={},
                error_json={},
                created_at="2026-07-13T00:00:00+00:00",
                updated_at="2026-07-13T00:00:00+00:00",
            )
        )
    return factory


def _turn_store(factory, *, run_id="run-1"):
    return SqliteSdkSessionStore.for_turn(
        factory,
        project_key="project-key",
        sdk_session_id="sdk-session",
        run_id=run_id,
    )


def test_turn_store_hides_staged_entries_until_atomic_promotion(tmp_path):
    factory = _factory_with_active_turn(tmp_path)
    turn_store = _turn_store(factory)
    committed_store = SqliteSdkSessionStore.committed(factory)
    main_key = {"project_key": "project-key", "session_id": "sdk-session"}
    subagent_key = {**main_key, "subpath": "subagents/agent-one"}
    entry = {"type": "user", "uuid": "entry-1", "message": {"content": "hello"}}

    asyncio.run(turn_store.append(main_key, [entry]))
    asyncio.run(turn_store.append(subagent_key, [{"type": "assistant", "uuid": "entry-2", "opaque": [1, {"x": True}]}]))

    assert asyncio.run(turn_store.load(main_key)) == [entry]
    assert asyncio.run(committed_store.load(main_key)) is None
    assert asyncio.run(committed_store.list_subkeys(main_key)) == []

    with factory.begin() as db:
        assert promote_staged_entries(db, run_id="run-1", committed_at="2026-07-13T00:01:00+00:00") == 2

    assert asyncio.run(committed_store.load(main_key)) == [entry]
    assert asyncio.run(committed_store.list_subkeys(main_key)) == ["subagents/agent-one"]


def test_uuid_append_is_idempotent_but_rejects_different_opaque_content(tmp_path):
    factory = _factory_with_active_turn(tmp_path)
    store = _turn_store(factory)
    key = {"project_key": "project-key", "session_id": "sdk-session"}
    entry = {"type": "assistant", "uuid": "stable-uuid", "payload": {"answer": 42}}

    asyncio.run(store.append(key, [entry]))
    asyncio.run(store.append(key, [entry]))
    with factory() as db:
        assert db.query(SdkSessionEntryModel).count() == 1

    with pytest.raises(SessionConflictError, match="reused with different content"):
        asyncio.run(store.append(key, [{**entry, "payload": {"answer": 43}}]))


def test_entries_without_uuid_preserve_append_order(tmp_path):
    factory = _factory_with_active_turn(tmp_path)
    store = _turn_store(factory)
    key = {"project_key": "project-key", "session_id": "sdk-session"}
    entries = [{"type": "title", "value": "one"}, {"type": "title", "value": "two"}]

    asyncio.run(store.append(key, entries))
    asyncio.run(store.append(key, [entries[0]]))

    assert asyncio.run(store.load(key)) == [entries[0], entries[1], entries[0]]


def test_turn_store_rejects_cross_session_keys_and_invalid_main_subpath(tmp_path):
    factory = _factory_with_active_turn(tmp_path)
    store = _turn_store(factory)

    with pytest.raises(SessionConflictError, match="does not match"):
        asyncio.run(
            store.append(
                {"project_key": "other-project", "session_id": "sdk-session"},
                [{"type": "user"}],
            )
        )
    with pytest.raises(ValueError, match="subpath must be omitted"):
        asyncio.run(
            store.append(
                {"project_key": "project-key", "session_id": "sdk-session", "subpath": ""},
                [{"type": "user"}],
            )
        )


@pytest.mark.parametrize("lost_boundary", ["intent", "lease", "expiry"])
def test_late_append_is_rejected_after_turn_ownership_is_lost(tmp_path, lost_boundary):
    factory = _factory_with_active_turn(tmp_path)
    store = _turn_store(factory)
    with factory.begin() as db:
        intent = db.get(SessionTurnIntentModel, "run-1")
        session = db.get(SessionRecordModel, "api-session")
        assert intent is not None and session is not None
        if lost_boundary == "intent":
            intent.status = "cancelled"
        elif lost_boundary == "lease":
            session.active_run_id = "run-other"
        else:
            session.active_run_expires_at = "2000-01-01T00:00:00+00:00"

    with pytest.raises(SessionConflictError):
        asyncio.run(
            store.append(
                {"project_key": "project-key", "session_id": "sdk-session"},
                [{"type": "assistant", "uuid": "late-entry"}],
            )
        )


def test_discarded_stage_never_becomes_visible_or_promotable(tmp_path):
    factory = _factory_with_active_turn(tmp_path)
    store = _turn_store(factory)
    committed = SqliteSdkSessionStore.committed(factory)
    key = {"project_key": "project-key", "session_id": "sdk-session"}
    asyncio.run(store.append(key, [{"type": "assistant", "uuid": "discard-me"}]))

    with factory.begin() as db:
        assert discard_staged_entries(db, run_id="run-1", discarded_at="2026-07-13T00:01:00+00:00") == 1
        assert promote_staged_entries(db, run_id="run-1", committed_at="2026-07-13T00:02:00+00:00") == 0

    assert asyncio.run(store.load(key)) is None
    assert asyncio.run(committed.load(key)) is None


def test_import_store_stages_without_a_runtime_intent(tmp_path):
    sessions = LocalSessionStore(tmp_path / "data" / "sessions")
    factory = sessions.Session
    expected_instance_etag = register_test_business_agent_instance(
        factory,
        agent_id=ORDINARY_TEST_AGENT_ID,
    )
    session = sessions.get_or_create_owned("api-session", agent_id=ORDINARY_TEST_AGENT_ID)
    session.sdk_session_id = "sdk-session"
    sessions.save(session)
    claim = sessions.begin_sdk_store_import(
        session_id=session.session_id,
        expected_instance_etag=expected_instance_etag,
        sdk_session_id="sdk-session",
        sdk_project_key="project-key",
    )
    assert claim is not None
    store = SqliteSdkSessionStore.for_import(
        factory,
        project_key="project-key",
        sdk_session_id="sdk-session",
        import_id=claim.token,
        session_id=claim.session_id,
        agent_id=claim.agent_id,
        expected_instance_etag=claim.expected_instance_etag,
        claim_marker=claim.marker,
    )
    committed = SqliteSdkSessionStore.committed(factory)
    key = {"project_key": "project-key", "session_id": "sdk-session"}

    asyncio.run(store.append(key, [{"type": "user", "uuid": "legacy-entry"}]))
    assert asyncio.run(committed.load(key)) is None
    with factory.begin() as db:
        promote_staged_entries(db, run_id=claim.token)
    assert asyncio.run(committed.load(key)) == [{"type": "user", "uuid": "legacy-entry"}]


def _stage_entry(
    factory,
    *,
    origin_run_id: str | None,
    entry_uuid: str,
    project_key: str = "orphan-project",
    sdk_session_id: str = "orphan-sdk",
) -> None:
    with factory.begin() as db:
        db.add(
            SdkSessionEntryModel(
                project_key=project_key,
                sdk_session_id=sdk_session_id,
                subpath="",
                entry_uuid=entry_uuid,
                entry_json={"type": "user", "uuid": entry_uuid},
                origin_run_id=origin_run_id,
                committed_at=None,
                discarded_at=None,
            )
        )


def test_orphan_reconciler_preserves_valid_turn_and_public_import_staging(tmp_path):
    turn_factory = _factory_with_active_turn(tmp_path / "turn")
    _stage_entry(
        turn_factory,
        origin_run_id="run-1",
        entry_uuid="valid-turn",
        project_key="project-key",
        sdk_session_id="sdk-session",
    )
    assert reconcile_orphaned_staged_entries(turn_factory, now="2026-07-13T00:01:00+00:00") == 0

    sessions = LocalSessionStore(tmp_path / "import" / "data" / "sessions")
    etag = register_test_business_agent_instance(sessions.Session, agent_id=ORDINARY_TEST_AGENT_ID)
    session = sessions.get_or_create_owned("valid-import", agent_id=ORDINARY_TEST_AGENT_ID)
    session.sdk_session_id = "valid-import-sdk"
    sessions.save(session)
    claim = sessions.begin_sdk_store_import(
        session_id=session.session_id,
        expected_instance_etag=etag,
        sdk_session_id="valid-import-sdk",
        sdk_project_key="valid-import-project",
        now="2026-07-13T00:00:00+00:00",
    )
    assert claim is not None
    _stage_entry(
        sessions.Session,
        origin_run_id=claim.token,
        entry_uuid="valid-import",
        project_key=claim.sdk_project_key,
        sdk_session_id=claim.sdk_session_id,
    )
    assert sessions.reconcile_orphaned_sdk_entries(now="2026-07-13T00:01:00+00:00") == 0


@pytest.mark.parametrize("orphan_kind", ["no-owner", "expired", "deleted", "token-mismatch"])
def test_orphan_reconciler_discards_unowned_import_staging_idempotently(tmp_path, orphan_kind):
    sessions = LocalSessionStore(tmp_path / "data" / "sessions")
    etag = register_test_business_agent_instance(sessions.Session, agent_id=ORDINARY_TEST_AGENT_ID)
    token: str | None = "unowned-token"
    if orphan_kind != "no-owner":
        session = sessions.get_or_create_owned("orphan-import", agent_id=ORDINARY_TEST_AGENT_ID)
        session.sdk_session_id = "orphan-sdk"
        sessions.save(session)
        claim = sessions.begin_sdk_store_import(
            session_id=session.session_id,
            expected_instance_etag=etag,
            sdk_session_id="orphan-sdk",
            sdk_project_key="orphan-project",
            lease_seconds=10,
            now="2026-07-13T00:00:00+00:00",
        )
        assert claim is not None
        token = "wrong-token" if orphan_kind == "token-mismatch" else claim.token
        if orphan_kind == "deleted":
            with sessions.Session.begin() as db:
                row = db.get(AgentRegistryModel, ORDINARY_TEST_AGENT_ID)
                assert row is not None
                row.deleted_at = "2026-07-13T00:00:01+00:00"
    _stage_entry(sessions.Session, origin_run_id=token, entry_uuid=f"orphan-{orphan_kind}")

    now = "2026-07-13T00:00:05+00:00" if orphan_kind != "expired" else "2026-07-13T00:00:11+00:00"
    assert sessions.reconcile_orphaned_sdk_entries(now=now) == 1
    assert sessions.reconcile_orphaned_sdk_entries(now=now) == 0
    with sessions.Session() as db:
        entry = db.query(SdkSessionEntryModel).filter_by(entry_uuid=f"orphan-{orphan_kind}").one()
        assert entry.committed_at is None
        assert entry.discarded_at == now


def test_orphan_reconciler_processes_multiple_bounded_projection_batches(tmp_path):
    sessions = LocalSessionStore(tmp_path / "data" / "sessions")
    total = 205
    with sessions.Session.begin() as db:
        db.add_all(
            SdkSessionEntryModel(
                project_key="orphan-project",
                sdk_session_id="orphan-sdk",
                subpath="",
                entry_uuid=f"bulk-orphan-{index}",
                entry_json={"type": "user", "payload": "x" * 4096},
                origin_run_id=f"orphan-{index}",
                committed_at=None,
                discarded_at=None,
            )
            for index in range(total)
        )

    engine = sessions.Session.kw["bind"]
    assert engine is not None

    def lower_sqlite_variable_limit(dbapi_connection, _connection_record) -> None:
        dbapi_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 128)

    engine.dispose()
    event.listen(engine, "connect", lower_sqlite_variable_limit)
    try:
        assert sessions.reconcile_orphaned_sdk_entries(now="2026-07-13T00:01:00+00:00") == total
    finally:
        event.remove(engine, "connect", lower_sqlite_variable_limit)
        engine.dispose()
    assert sessions.reconcile_orphaned_sdk_entries(now="2026-07-13T00:01:00+00:00") == 0
    with sessions.Session() as db:
        assert db.query(SdkSessionEntryModel).filter(SdkSessionEntryModel.discarded_at.is_(None)).count() == 0


def test_committed_session_store_uses_persisted_project_binding_after_workspace_changes(tmp_path):
    factory = _factory_with_active_turn(tmp_path)
    turn_store = _turn_store(factory)
    persisted_key = {"project_key": "project-key", "session_id": "sdk-session"}
    entry = {"type": "user", "uuid": "candidate-history", "message": {"content": "hello"}}
    asyncio.run(turn_store.append(persisted_key, [entry]))
    with factory.begin() as db:
        promote_staged_entries(db, run_id="run-1")
    history_store = SqliteSdkSessionStore.for_committed_session(
        factory,
        project_key="project-key",
        sdk_session_id="sdk-session",
    )

    assert asyncio.run(history_store.load({"project_key": "current-workspace-key", "session_id": "sdk-session"})) == [entry]
    with pytest.raises(SessionConflictError, match="does not match"):
        asyncio.run(history_store.load({"project_key": "current-workspace-key", "session_id": "sdk-other"}))
    with pytest.raises(PermissionError, match="read-only"):
        asyncio.run(history_store.append(persisted_key, [entry]))
