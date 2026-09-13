import gc
from collections.abc import Mapping
from pathlib import Path

import pytest
from app.runtime import runtime_db
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import Base, make_session_factory
from app.runtime_gateway.hitl_migration import HITL_FINGERPRINT_DATA_MIGRATION
from app.runtime_gateway.models import (
    AgentRunModel,
    RuntimeChatOperationModel,
    RuntimePendingActionModel,
    RuntimeSessionBindingModel,
    RuntimeSessionCreationIntentModel,
)
from app.runtime_gateway.operation_identity import (
    LEGACY_UNKNOWN_SESSION_REQUEST_FINGERPRINT,
    canonical_request_fingerprint,
    initial_operation_key,
    session_creation_request_fingerprint,
)
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from tests.runtime_schema_test_utils import (
    V1_EPOCH,
    V2_EPOCH,
    V3_EPOCH,
    convert_current_to_v1,
    convert_current_to_v2,
)

CURRENT_EPOCH = V3_EPOCH
LEGACY_EPOCH = V1_EPOCH


def _schema_marker_rows(engine: Engine) -> Mapping[str, str]:
    with engine.connect() as connection:
        return {
            str(version): str(applied_at)
            for version, applied_at in connection.execute(
                text("SELECT version, applied_at FROM schema_migrations"),
            )
        }


def _prepare_previous_epoch(
    db_path: Path,
    *,
    keep_hitl_marker: bool,
    include_removed_release_table: bool = True,
    nonempty_release_operations: bool = False,
    extra_intent_column: bool = False,
    malformed_release_index: bool = False,
):
    factory = make_session_factory(db_path)
    engine = factory.kw["bind"]
    engine.dispose()
    convert_current_to_v1(
        db_path,
        include_removed_release_table=include_removed_release_table,
        malformed_release_index=malformed_release_index,
    )
    with engine.begin() as connection:
        if extra_intent_column:
            connection.execute(
                text(
                    "ALTER TABLE runtime_session_creation_intents ADD COLUMN unknown_identity TEXT",
                ),
            )
        connection.execute(
            text("UPDATE schema_migrations SET applied_at = 'legacy-epoch' WHERE version = :version"),
            {"version": LEGACY_EPOCH},
        )
        if keep_hitl_marker:
            connection.execute(
                text(
                    "UPDATE schema_migrations SET applied_at = 'preserved-hitl' WHERE version = :version",
                ),
                {"version": HITL_FINGERPRINT_DATA_MIGRATION},
            )
        else:
            connection.execute(
                text("DELETE FROM schema_migrations WHERE version = :version"),
                {"version": HITL_FINGERPRINT_DATA_MIGRATION},
            )
        _insert_previous_epoch_rows(
            connection,
            include_removed_release_table=include_removed_release_table,
            nonempty_release_operations=nonempty_release_operations,
        )
    return engine


def _insert_previous_epoch_rows(
    connection,
    *,
    include_removed_release_table: bool,
    nonempty_release_operations: bool,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO runtime_session_creation_intents (
                intent_id, idempotency_key, agent_id, agent_version_id,
                runtime_agent_id, harness_digest, workspace_id,
                session_name, session_id, status, error_json,
                cleanup_attempts, created_at, updated_at, completed_at
            ) VALUES (
                'intent-v1', 'operation-v1', 'agent-v1', 'version-v1',
                'runtime-v1', :digest, 'workspace-v1',
                'title-owned-by-agentscope', 'session-v1',
                'cleanup_pending', :error_json, 3,
                'created-v1', 'updated-v1', NULL
            )
            """,
        ),
        {"digest": "d" * 64, "error_json": '{"type":"retry"}'},
    )
    if nonempty_release_operations:
        assert include_removed_release_table
        connection.execute(
            text(
                """
                INSERT INTO agent_releases (
                    release_id, agent_id, created_at, updated_at, status,
                    tag_name, commit_sha, payload_json
                ) VALUES (
                    'release-with-history', 'agent-v1', 'created-v1',
                    'updated-v1', 'published', 'v1', :commit_sha, '{}'
                )
                """,
            ),
            {"commit_sha": "c" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO agent_release_operations (
                    operation_id, agent_id, release_id, operation_kind,
                    status, expected_head_sha, target_commit_sha,
                    release_expected_status, release_expected_updated_at,
                    claim_generation, operator, result_json, error_json,
                    created_at, updated_at
                ) VALUES (
                    'operation-with-history', 'agent-v1',
                    'release-with-history', 'restore', 'completed',
                    :expected_sha, :target_sha, 'published', 'release-v1',
                    0, 'operator-v1', '{}', '{}', 'created-v1', 'updated-v1'
                )
                """,
            ),
            {"expected_sha": "e" * 64, "target_sha": "t" * 64},
        )


def _prepare_v2_epoch(db_path: Path) -> tuple[Engine, JsonObject]:
    factory = make_session_factory(db_path)
    engine = factory.kw["bind"]
    engine.dispose()
    convert_current_to_v2(db_path)
    request: JsonObject = {"role": "user", "content": []}
    request_fingerprint = canonical_request_fingerprint(
        {"input": request, "metadata": {}},
    )
    with engine.begin() as connection:
        _insert_v2_session_facts(connection)
        _insert_v2_run_facts(connection, request_fingerprint=request_fingerprint)
    return engine, request


def _insert_v2_session_facts(connection: Connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO runtime_session_creation_intents (
                intent_id, idempotency_key, agent_id, agent_version_id,
                runtime_agent_id, harness_digest, workspace_id, session_id,
                status, error_json, cleanup_attempts, created_at, updated_at,
                completed_at
            ) VALUES (
                'intent-v2', 'session-operation-v2', 'agent-v2',
                'version-v2', 'runtime-v2', :digest, 'workspace-v2',
                'session-created-v2', 'completed', NULL, 0,
                'created-v2', 'updated-v2', 'completed-v2'
            )
            """,
        ),
        {"digest": "e" * 64},
    )
    connection.execute(
        text(
            """
            INSERT INTO runtime_session_bindings (
                session_id, agent_id, agent_version_id, runtime_agent_id,
                harness_digest, root_session_id, team_id, active_run_id,
                active_team_generation, created_at, updated_at
            ) VALUES (
                'session-run-v2', 'agent-v2', 'version-v2', 'runtime-v2',
                :digest, 'session-run-v2', NULL, 'run-v2', 0,
                'created-v2', 'updated-v2'
            )
            """,
        ),
        {"digest": "e" * 64},
    )


def _insert_v2_run_facts(
    connection: Connection,
    *,
    request_fingerprint: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO agent_runs (
                run_id, session_id, agent_id, agent_version_id,
                runtime_agent_id, harness_digest, client_operation_id,
                input_fingerprint, trigger_response_status,
                trigger_response_body, trigger_response_content_type,
                status, reply_ids_json, persisted_reply_ids_json,
                persistence_batch_reply_ids_json, team_generation,
                root_persisted_team_generation, pending_child_session_ids_json,
                trace_id, trace_url, trace_status, terminal_reason, error_json,
                alert_id, case_id, metadata_json, created_at, started_at,
                updated_at, completed_at
            ) VALUES (
                'run-v2', 'session-run-v2', 'agent-v2', 'version-v2',
                'runtime-v2', :digest, 'chat-operation-v2',
                :request_fingerprint, 202, :response_body,
                'application/json', 'waiting_human', '[]', '[]', '[]', 0, 0,
                '[]', :trace_id, NULL, 'pending', NULL, NULL, NULL,
                NULL, '{}', 'created-v2', 'started-v2', 'updated-v2',
                NULL
            )
            """,
        ),
        {
            "digest": "e" * 64,
            "request_fingerprint": request_fingerprint,
            "response_body": b'{"session_id":"session-run-v2"}',
            "trace_id": "f" * 32,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO runtime_pending_actions (
                action_id, session_id, run_id, reply_id, tool_call_id,
                kind, tool_call_name, tool_call_json, status,
                run_rules_json, run_rules_granted_at, run_rules_expired_at,
                created_at, updated_at
            ) VALUES (
                'run-v2:reply-v2:tool-v2', 'session-run-v2', 'run-v2',
                'reply-v2', 'tool-v2', 'human', 'Read', '{}', 'pending',
                '[]', NULL, NULL, 'created-v2', 'updated-v2'
            )
            """,
        ),
    )


def test_fresh_runtime_database_has_only_agentscope_epoch_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"

    factory = make_session_factory(db_path)

    inspector = inspect(factory.kw["bind"])
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
    assert not any("claude" in name or "sdk" in name for name in inspector.get_table_names())
    assert set(_schema_marker_rows(factory.kw["bind"])) == {
        CURRENT_EPOCH,
        HITL_FINGERPRINT_DATA_MIGRATION,
    }


@pytest.mark.parametrize("keep_hitl_marker", [False, True])
@pytest.mark.parametrize("include_removed_release_table", [False, True])
def test_exact_previous_epoch_migrates_without_session_title_or_lost_governance_refs(
    tmp_path: Path,
    keep_hitl_marker: bool,
    include_removed_release_table: bool,
) -> None:
    db_path = tmp_path / "previous.sqlite3"
    engine = _prepare_previous_epoch(
        db_path,
        keep_hitl_marker=keep_hitl_marker,
        include_removed_release_table=include_removed_release_table,
    )

    factory = make_session_factory(db_path)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
    assert "agent_release_operations" not in inspector.get_table_names()
    assert "session_name" not in {column["name"] for column in inspector.get_columns("runtime_session_creation_intents")}
    with factory() as session:
        intent = session.get(RuntimeSessionCreationIntentModel, "intent-v1")
        assert intent is not None
        assert intent.request_fingerprint == session_creation_request_fingerprint(
            "runtime-v1",
            "title-owned-by-agentscope",
        )
        assert (
            intent.idempotency_key,
            intent.agent_id,
            intent.agent_version_id,
            intent.runtime_agent_id,
            intent.harness_digest,
            intent.workspace_id,
            intent.session_id,
            intent.status,
            intent.error_json,
            intent.cleanup_attempts,
            intent.created_at,
            intent.updated_at,
            intent.completed_at,
        ) == (
            "operation-v1",
            "agent-v1",
            "version-v1",
            "runtime-v1",
            "d" * 64,
            "workspace-v1",
            "session-v1",
            "cleanup_pending",
            {"type": "retry"},
            3,
            "created-v1",
            "updated-v1",
            None,
        )
    markers = _schema_marker_rows(engine)
    assert set(markers) == {CURRENT_EPOCH, HITL_FINGERPRINT_DATA_MIGRATION}
    if keep_hitl_marker:
        assert markers[HITL_FINGERPRINT_DATA_MIGRATION] == "preserved-hitl"

    repeated = make_session_factory(db_path)
    assert _schema_marker_rows(repeated.kw["bind"]) == markers
    with repeated() as session:
        assert session.get(RuntimeSessionCreationIntentModel, "intent-v1") is not None


def test_exact_v2_epoch_migrates_response_ledger_and_preserves_runtime_facts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    engine, request = _prepare_v2_epoch(db_path)

    factory = make_session_factory(db_path)

    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
    assert {
        "trigger_response_status",
        "trigger_response_body",
        "trigger_response_content_type",
    }.isdisjoint(
        {column["name"] for column in inspector.get_columns("agent_runs")},
    )
    with factory() as session:
        intent = session.get(RuntimeSessionCreationIntentModel, "intent-v2")
        binding = session.get(RuntimeSessionBindingModel, "session-run-v2")
        run = session.get(AgentRunModel, "run-v2")
        action = session.get(
            RuntimePendingActionModel,
            "run-v2:reply-v2:tool-v2",
        )
        operation = session.get(
            RuntimeChatOperationModel,
            initial_operation_key("chat-operation-v2"),
        )
        assert intent is not None
        assert intent.request_fingerprint == LEGACY_UNKNOWN_SESSION_REQUEST_FINGERPRINT
        assert binding is not None and binding.active_run_id == "run-v2"
        assert run is not None and run.status == "waiting_human"
        assert action is not None and action.status == "pending"
        assert operation is not None
        assert operation.run_id == "run-v2"
        assert operation.operation_kind == "initial"
        assert operation.response_status == 202
        assert operation.response_body == b'{"session_id":"session-run-v2"}'
        assert operation.response_content_type == "application/json"
        assert operation.response_headers_json == {}

    store = RuntimeRunStore(factory)
    replay = store.admit_run(
        session_id="session-run-v2",
        runtime_agent_id="runtime-v2",
        input_value=request,
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="chat-operation-v2",
    )
    assert replay.should_trigger_upstream is False
    assert replay.replay_response is not None
    assert replay.replay_response.body == b'{"session_id":"session-run-v2"}'
    with pytest.raises(RuntimeStateConflict, match="no replay-safe request identity"):
        store.session_creation_for_request(
            key="session-operation-v2",
            runtime_agent_id="runtime-v2",
            requested_name=None,
        )


def test_v2_partial_response_ledger_is_rejected_without_partial_migration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v2-partial-response.sqlite3"
    engine, _request = _prepare_v2_epoch(db_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_runs SET trigger_response_content_type = NULL WHERE run_id = 'run-v2'",
            ),
        )
    isolated = create_engine(f"sqlite:///{db_path}")

    with pytest.raises(RuntimeError, match="partial trigger response ledger"):
        runtime_db.ensure_schema(isolated)

    inspector = inspect(isolated)
    assert "runtime_chat_operations" not in inspector.get_table_names()
    assert "trigger_response_status" in {column["name"] for column in inspector.get_columns("agent_runs")}
    assert set(_schema_marker_rows(isolated)) == {
        V2_EPOCH,
        HITL_FINGERPRINT_DATA_MIGRATION,
    }


def test_previous_epoch_refuses_nonempty_removed_release_operation_table_atomically(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "release-history.sqlite3"
    _prepare_previous_epoch(
        db_path,
        keep_hitl_marker=True,
        nonempty_release_operations=True,
    )
    isolated_engine = create_engine(f"sqlite:///{db_path}")

    with pytest.raises(RuntimeError, match="refusing to discard governance history"):
        runtime_db.ensure_schema(isolated_engine)

    inspector = inspect(isolated_engine)
    assert "agent_release_operations" in inspector.get_table_names()
    assert "session_name" in {column["name"] for column in inspector.get_columns("runtime_session_creation_intents")}
    assert set(_schema_marker_rows(isolated_engine)) == {
        LEGACY_EPOCH,
        HITL_FINGERPRINT_DATA_MIGRATION,
    }


def test_previous_epoch_refuses_unexpected_column_shape_without_partial_migration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "unexpected-column.sqlite3"
    _prepare_previous_epoch(
        db_path,
        keep_hitl_marker=False,
        extra_intent_column=True,
    )
    isolated_engine = create_engine(f"sqlite:///{db_path}")

    with pytest.raises(RuntimeError, match="physical schema contract mismatch"):
        runtime_db.ensure_schema(isolated_engine)

    inspector = inspect(isolated_engine)
    intent_columns = {column["name"] for column in inspector.get_columns("runtime_session_creation_intents")}
    assert {"session_name", "unknown_identity"} <= intent_columns
    assert "agent_release_operations" in inspector.get_table_names()
    assert set(_schema_marker_rows(isolated_engine)) == {LEGACY_EPOCH}


def test_previous_epoch_refuses_malformed_removed_table_without_partial_migration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "malformed-removed-table.sqlite3"
    _prepare_previous_epoch(
        db_path,
        keep_hitl_marker=True,
        malformed_release_index=True,
    )
    isolated_engine = create_engine(f"sqlite:///{db_path}")

    with pytest.raises(RuntimeError, match="physical schema contract mismatch"):
        runtime_db.ensure_schema(isolated_engine)

    inspector = inspect(isolated_engine)
    assert "agent_release_operations" in inspector.get_table_names()
    assert "session_name" in {column["name"] for column in inspector.get_columns("runtime_session_creation_intents")}
    assert set(_schema_marker_rows(isolated_engine)) == {
        LEGACY_EPOCH,
        HITL_FINGERPRINT_DATA_MIGRATION,
    }


def test_current_epoch_refuses_unknown_data_migration_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown-marker.sqlite3"
    factory = make_session_factory(db_path)
    with factory.kw["bind"].begin() as connection:
        connection.execute(
            text(
                "INSERT INTO schema_migrations(version, applied_at) VALUES ('unknown-data-migration', 'unknown')",
            ),
        )

    with pytest.raises(RuntimeError, match="unknown schema migration markers"):
        runtime_db.ensure_schema(create_engine(f"sqlite:///{db_path}"))


def test_fresh_schema_refuses_unknown_legacy_table_without_mutating_it(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("create table sdk_sessions (id text primary key)"))

    with pytest.raises(RuntimeError, match="unknown=.*sdk_sessions"):
        make_session_factory(db_path)

    assert inspect(engine).get_table_names() == ["sdk_sessions"]


def test_fresh_schema_refuses_partial_or_mismatched_epoch(tmp_path: Path) -> None:
    partial_path = tmp_path / "partial.sqlite3"
    partial = create_engine(f"sqlite:///{partial_path}")
    with partial.begin() as connection:
        connection.execute(text("create table schema_migrations (version text primary key, applied_at text)"))
    with pytest.raises(RuntimeError, match="missing or conflicting schema epoch"):
        make_session_factory(partial_path)

    current_path = tmp_path / "current.sqlite3"
    factory = make_session_factory(current_path)
    with factory.kw["bind"].begin() as connection:
        connection.execute(text("alter table agent_runs add column legacy_runtime_id text"))
    with pytest.raises(RuntimeError, match="physical schema contract mismatch"):
        # Bypass the path engine cache to prove the on-disk schema is re-inspected.
        from app.runtime.runtime_db import ensure_schema

        ensure_schema(create_engine(f"sqlite:///{current_path}"))


def test_engine_cache_reuses_live_factory_without_owning_transient_engines(tmp_path: Path) -> None:
    shared_path = tmp_path / "shared.sqlite3"
    first = make_session_factory(shared_path)
    second = make_session_factory(shared_path)
    assert first.kw["bind"] is second.kw["bind"]

    transient_paths: list[Path] = []
    for index in range(32):
        db_path = tmp_path / f"transient-{index}.sqlite3"
        transient_paths.append(db_path.resolve())
        factory = make_session_factory(db_path)
        with factory() as session:
            assert session.execute(text("select 1")).scalar_one() == 1
    del session, factory
    gc.collect()

    assert shared_path.resolve() in runtime_db._ENGINE_CACHE
    assert not set(transient_paths).intersection(runtime_db._ENGINE_CACHE)
