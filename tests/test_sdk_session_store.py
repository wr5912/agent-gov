import asyncio

import pytest
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
)

from business_agent_test_utils import ORDINARY_TEST_AGENT_ID


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
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = SqliteSdkSessionStore.for_import(
        factory,
        project_key="project-key",
        sdk_session_id="sdk-session",
        import_id="import:one",
    )
    committed = SqliteSdkSessionStore.committed(factory)
    key = {"project_key": "project-key", "session_id": "sdk-session"}

    asyncio.run(store.append(key, [{"type": "user", "uuid": "legacy-entry"}]))
    assert asyncio.run(committed.load(key)) is None
    with factory.begin() as db:
        promote_staged_entries(db, run_id="import:one")
    assert asyncio.run(committed.load(key)) == [{"type": "user", "uuid": "legacy-entry"}]


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
