import asyncio
import json
import uuid

import pytest
from app.runtime.errors import RuntimeUnavailableError, SessionConflictError
from app.runtime.runtime_db import SdkSessionEntryModel
from app.runtime.sdk_session_migration import ensure_sdk_store_ready
from app.runtime.sdk_session_store import SqliteSdkSessionStore
from app.runtime.session_store import LocalSessionStore
from claude_agent_sdk import get_session_messages_from_store, project_key_for_directory

from business_agent_test_utils import ORDINARY_TEST_AGENT_ID


def _legacy_session(tmp_path):
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "claude-root" / ".claude"
    workspace.mkdir(parents=True)
    sdk_session_id = str(uuid.uuid4())
    project_key = project_key_for_directory(str(workspace))
    transcript = config_dir / "projects" / project_key / f"{sdk_session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    entries = [
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "parentUuid": "user-1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        },
    ]
    transcript.write_text("".join(f"{json.dumps(entry)}\n" for entry in entries), encoding="utf-8")
    store = LocalSessionStore(tmp_path / "data" / "sessions")
    session = store.get_or_create_owned("api-session", agent_id=ORDINARY_TEST_AGENT_ID)
    session.sdk_session_id = sdk_session_id
    store.save(session)
    return store, store.get("api-session"), workspace, config_dir, sdk_session_id, project_key


def test_legacy_import_promotes_official_sdk_transcript_once(tmp_path):
    store, session, workspace, config_dir, sdk_session_id, project_key = _legacy_session(tmp_path)
    assert session is not None

    ready = asyncio.run(
        ensure_sdk_store_ready(
            store,
            session,
            workspace_dir=workspace,
            claude_config_dir=config_dir,
        )
    )
    repeated = asyncio.run(
        ensure_sdk_store_ready(
            store,
            ready,
            workspace_dir=workspace,
            claude_config_dir=config_dir,
        )
    )

    assert repeated.sdk_store_ready_at == ready.sdk_store_ready_at
    assert repeated.sdk_project_key == project_key
    committed = SqliteSdkSessionStore.committed(store.Session)
    messages = asyncio.run(
        get_session_messages_from_store(
            committed,
            sdk_session_id,
            directory=str(workspace),
        )
    )
    assert [message.type for message in messages] == ["user", "assistant"]


def test_legacy_import_failure_discards_partial_stage_and_fails_closed(tmp_path, monkeypatch):
    store, session, workspace, config_dir, sdk_session_id, project_key = _legacy_session(tmp_path)
    assert session is not None

    async def partial_import(session_id, adapter, **kwargs):
        await adapter.append(
            {"project_key": project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "partial-import"}],
        )
        raise OSError("injected import failure")

    monkeypatch.setattr("app.runtime.sdk_session_migration.sdk.import_session_to_store", partial_import)

    with pytest.raises(RuntimeUnavailableError, match="migration failed"):
        asyncio.run(
            ensure_sdk_store_ready(
                store,
                session,
                workspace_dir=workspace,
                claude_config_dir=config_dir,
            )
        )

    failed = store.get("api-session")
    assert failed is not None
    assert failed.sdk_store_ready_at is None
    assert failed.sdk_store_migration_error == "OSError: injected import failure"
    assert asyncio.run(SqliteSdkSessionStore.committed(store.Session).load({"project_key": project_key, "session_id": sdk_session_id})) is None


def test_import_claim_is_cross_connection_fenced_and_expired_owner_cannot_finalize(tmp_path):
    store, session, workspace, _, sdk_session_id, project_key = _legacy_session(tmp_path)
    assert session is not None
    other_process = LocalSessionStore(store.root)
    first = store.begin_sdk_store_import(
        session_id=session.session_id,
        sdk_session_id=sdk_session_id,
        sdk_project_key=project_key,
        lease_seconds=10,
        now="2026-07-13T00:00:00+00:00",
    )
    assert first is not None
    first_adapter = SqliteSdkSessionStore.for_import(
        store.Session,
        project_key=project_key,
        sdk_session_id=sdk_session_id,
        import_id=first.token,
    )
    asyncio.run(
        first_adapter.append(
            {"project_key": project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "first-owner-entry"}],
        )
    )

    with pytest.raises(SessionConflictError, match="already running"):
        other_process.begin_sdk_store_import(
            session_id=session.session_id,
            sdk_session_id=sdk_session_id,
            sdk_project_key=project_key,
            lease_seconds=10,
            now="2026-07-13T00:00:05+00:00",
        )

    second = other_process.begin_sdk_store_import(
        session_id=session.session_id,
        sdk_session_id=sdk_session_id,
        sdk_project_key=project_key,
        lease_seconds=10,
        now="2026-07-13T00:00:11+00:00",
    )
    assert second is not None and second.token != first.token
    second_adapter = SqliteSdkSessionStore.for_import(
        other_process.Session,
        project_key=project_key,
        sdk_session_id=sdk_session_id,
        import_id=second.token,
    )
    asyncio.run(
        second_adapter.append(
            {"project_key": project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "second-owner-entry"}],
        )
    )
    with pytest.raises(SessionConflictError, match="fence was lost"):
        store.complete_sdk_store_import(claim=first, now="2026-07-13T00:00:12+00:00")
    assert store.fail_sdk_store_import(claim=first, error="old owner") is False
    assert other_process.get(session.session_id).sdk_store_migration_error == second.marker

    ready = other_process.complete_sdk_store_import(
        claim=second,
        now="2026-07-13T00:00:12+00:00",
    )
    assert ready.sdk_store_ready_at == "2026-07-13T00:00:12+00:00"


@pytest.mark.parametrize("operation", ["clear", "delete"])
def test_mapping_invalidation_discards_inflight_import_staging(tmp_path, operation):
    store, session, _, _, sdk_session_id, project_key = _legacy_session(tmp_path)
    assert session is not None
    claim = store.begin_sdk_store_import(
        session_id=session.session_id,
        sdk_session_id=sdk_session_id,
        sdk_project_key=project_key,
    )
    assert claim is not None
    adapter = SqliteSdkSessionStore.for_import(
        store.Session,
        project_key=project_key,
        sdk_session_id=sdk_session_id,
        import_id=claim.token,
    )
    asyncio.run(
        adapter.append(
            {"project_key": project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": f"inflight-{operation}"}],
        )
    )

    current = store.get(session.session_id)
    assert current is not None
    if operation == "clear":
        store.clear_sdk_session(current, agent_id=ORDINARY_TEST_AGENT_ID)
    else:
        assert store.delete(session.session_id) is True

    with store.Session() as db:
        staged = db.query(SdkSessionEntryModel).filter_by(origin_run_id=claim.token).one()
        assert staged.committed_at is None
        assert staged.discarded_at is not None
    assert store.fail_sdk_store_import(claim=claim, error="late importer") is False
