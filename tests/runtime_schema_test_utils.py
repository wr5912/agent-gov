from __future__ import annotations

import sqlite3
from pathlib import Path

V3_EPOCH = "agentscope-runtime-v3"
V2_EPOCH = "agentscope-runtime-v2"
V1_EPOCH = "agentscope-runtime-v1"


def convert_current_to_v2(db_path: Path) -> None:
    """Reverse a fresh v3 fixture into the frozen, exact v2 physical shape."""

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE runtime_chat_operations")
        _rebuild_session_intents(connection, include_session_name=False)
        _rebuild_agent_runs_with_legacy_response_slots(connection)
        connection.execute(
            "UPDATE schema_migrations SET version = ? WHERE version = ?",
            (V2_EPOCH, V3_EPOCH),
        )


def convert_v2_to_v1(
    db_path: Path,
    *,
    include_removed_release_table: bool,
    malformed_release_index: bool = False,
) -> None:
    """Reverse an exact v2 fixture into either accepted v1 table set."""

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        _rebuild_session_intents(connection, include_session_name=True)
        connection.execute(
            "UPDATE schema_migrations SET version = ? WHERE version = ?",
            (V1_EPOCH, V2_EPOCH),
        )
        if include_removed_release_table:
            create_removed_release_operation_table(
                connection,
                malformed_index=malformed_release_index,
            )


def convert_current_to_v1(
    db_path: Path,
    *,
    include_removed_release_table: bool,
    malformed_release_index: bool = False,
) -> None:
    convert_current_to_v2(db_path)
    convert_v2_to_v1(
        db_path,
        include_removed_release_table=include_removed_release_table,
        malformed_release_index=malformed_release_index,
    )


def create_removed_release_operation_table(
    connection: sqlite3.Connection,
    *,
    malformed_index: bool = False,
) -> None:
    connection.executescript(
        """
        CREATE TABLE agent_release_operations (
            operation_id VARCHAR(128) NOT NULL PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            release_id VARCHAR(128) NOT NULL,
            operation_kind VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            expected_head_sha VARCHAR(64) NOT NULL,
            target_commit_sha VARCHAR(64) NOT NULL,
            release_expected_status VARCHAR(64) NOT NULL,
            release_expected_updated_at VARCHAR(64) NOT NULL,
            claim_token VARCHAR(128),
            claim_generation INTEGER NOT NULL,
            claim_expires_at VARCHAR(64),
            operator VARCHAR(128) NOT NULL,
            note TEXT,
            previous_head_sha VARCHAR(64),
            observed_head_sha VARCHAR(64),
            result_json JSON NOT NULL,
            error_json JSON NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64),
            FOREIGN KEY(release_id) REFERENCES agent_releases (release_id)
        );
        CREATE INDEX ix_agent_release_operations_agent_id
            ON agent_release_operations (agent_id);
        CREATE INDEX ix_agent_release_operations_claim_expires_at
            ON agent_release_operations (claim_expires_at);
        CREATE INDEX ix_agent_release_operations_claim_token
            ON agent_release_operations (claim_token);
        CREATE INDEX ix_agent_release_operations_created_at
            ON agent_release_operations (created_at);
        CREATE INDEX ix_agent_release_operations_operation_kind
            ON agent_release_operations (operation_kind);
        CREATE INDEX ix_agent_release_operations_release_id
            ON agent_release_operations (release_id);
        CREATE INDEX ix_agent_release_operations_status
            ON agent_release_operations (status);
        CREATE INDEX ix_agent_release_operations_updated_at
            ON agent_release_operations (updated_at);
        """,
    )
    if not malformed_index:
        connection.execute(
            "CREATE UNIQUE INDEX ux_agent_release_operations_identity ON agent_release_operations (operation_kind, release_id, expected_head_sha)",
        )


def _drop_named_indexes(connection: sqlite3.Connection, table_name: str) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
        (table_name,),
    ).fetchall()
    for (index_name,) in rows:
        connection.execute(f'DROP INDEX "{index_name}"')


def _rebuild_session_intents(
    connection: sqlite3.Connection,
    *,
    include_session_name: bool,
) -> None:
    table_name = "runtime_session_creation_intents"
    backup_name = f"{table_name}__schema_fixture"
    _drop_named_indexes(connection, table_name)
    connection.execute(f'ALTER TABLE "{table_name}" RENAME TO "{backup_name}"')
    optional_name = "session_name VARCHAR(512)," if include_session_name else ""
    connection.execute(
        f"""
        CREATE TABLE {table_name} (
            intent_id VARCHAR(128) NOT NULL PRIMARY KEY,
            idempotency_key VARCHAR(256),
            agent_id VARCHAR(128) NOT NULL,
            agent_version_id VARCHAR(256) NOT NULL,
            runtime_agent_id VARCHAR(128) NOT NULL,
            harness_digest VARCHAR(64) NOT NULL,
            workspace_id VARCHAR(320) NOT NULL,
            {optional_name}
            session_id VARCHAR(128),
            status VARCHAR(32) NOT NULL,
            error_json JSON,
            cleanup_attempts INTEGER NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64)
        )
        """,
    )
    copied = (
        "intent_id, idempotency_key, agent_id, agent_version_id, "
        "runtime_agent_id, harness_digest, workspace_id, session_id, status, "
        "error_json, cleanup_attempts, created_at, updated_at, completed_at"
    )
    connection.execute(
        f'INSERT INTO "{table_name}" ({copied}) SELECT {copied} FROM "{backup_name}"',
    )
    connection.execute(f'DROP TABLE "{backup_name}"')
    _create_session_intent_indexes(connection)


def _create_session_intent_indexes(connection: sqlite3.Connection) -> None:
    indexes = (
        "CREATE INDEX ix_runtime_session_creation_intents_agent_id ON runtime_session_creation_intents (agent_id)",
        "CREATE INDEX ix_runtime_session_creation_intents_agent_version_id ON runtime_session_creation_intents (agent_version_id)",
        "CREATE INDEX ix_runtime_session_creation_intents_created_at ON runtime_session_creation_intents (created_at)",
        "CREATE UNIQUE INDEX ix_runtime_session_creation_intents_idempotency_key ON runtime_session_creation_intents (idempotency_key)",
        "CREATE INDEX ix_runtime_session_creation_intents_runtime_agent_id ON runtime_session_creation_intents (runtime_agent_id)",
        "CREATE INDEX ix_runtime_session_creation_intents_session_id ON runtime_session_creation_intents (session_id)",
        "CREATE INDEX ix_runtime_session_creation_intents_status ON runtime_session_creation_intents (status)",
        "CREATE INDEX ix_runtime_session_creation_intents_updated_at ON runtime_session_creation_intents (updated_at)",
        "CREATE INDEX ix_runtime_session_creation_intents_workspace_id ON runtime_session_creation_intents (workspace_id)",
        "CREATE INDEX ix_runtime_session_intents_recovery ON runtime_session_creation_intents (status, updated_at)",
    )
    for statement in indexes:
        connection.execute(statement)


def _rebuild_agent_runs_with_legacy_response_slots(
    connection: sqlite3.Connection,
) -> None:
    table_name = "agent_runs"
    backup_name = f"{table_name}__schema_fixture"
    _drop_named_indexes(connection, table_name)
    connection.execute(f'ALTER TABLE "{table_name}" RENAME TO "{backup_name}"')
    connection.execute(
        """
        CREATE TABLE agent_runs (
            run_id VARCHAR(128) NOT NULL PRIMARY KEY,
            session_id VARCHAR(128) NOT NULL,
            agent_id VARCHAR(128) NOT NULL,
            agent_version_id VARCHAR(256) NOT NULL,
            runtime_agent_id VARCHAR(128) NOT NULL,
            harness_digest VARCHAR(64) NOT NULL,
            client_operation_id VARCHAR(128),
            input_fingerprint VARCHAR(64),
            trigger_response_status INTEGER,
            trigger_response_body BLOB,
            trigger_response_content_type VARCHAR(256),
            status VARCHAR(32) NOT NULL,
            reply_ids_json JSON NOT NULL,
            persisted_reply_ids_json JSON NOT NULL,
            persistence_batch_reply_ids_json JSON NOT NULL,
            team_generation INTEGER NOT NULL,
            root_persisted_team_generation INTEGER NOT NULL,
            pending_child_session_ids_json JSON NOT NULL,
            trace_id VARCHAR(64),
            trace_url VARCHAR(2048),
            trace_status VARCHAR(32) NOT NULL,
            terminal_reason VARCHAR(64),
            error_json JSON,
            alert_id VARCHAR(256),
            case_id VARCHAR(256),
            metadata_json JSON NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            started_at VARCHAR(64),
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64)
        )
        """,
    )
    copied = (
        "run_id, session_id, agent_id, agent_version_id, runtime_agent_id, "
        "harness_digest, client_operation_id, input_fingerprint, status, "
        "reply_ids_json, persisted_reply_ids_json, "
        "persistence_batch_reply_ids_json, team_generation, "
        "root_persisted_team_generation, pending_child_session_ids_json, "
        "trace_id, trace_url, trace_status, terminal_reason, error_json, "
        "alert_id, case_id, metadata_json, created_at, started_at, updated_at, "
        "completed_at"
    )
    connection.execute(
        f'INSERT INTO "{table_name}" ({copied}) SELECT {copied} FROM "{backup_name}"',
    )
    connection.execute(f'DROP TABLE "{backup_name}"')
    _create_agent_run_indexes(connection)


def _create_agent_run_indexes(connection: sqlite3.Connection) -> None:
    indexes = (
        "CREATE INDEX ix_agent_runs_agent_id ON agent_runs (agent_id)",
        "CREATE INDEX ix_agent_runs_agent_version_id ON agent_runs (agent_version_id)",
        "CREATE INDEX ix_agent_runs_alert_id ON agent_runs (alert_id)",
        "CREATE INDEX ix_agent_runs_case_id ON agent_runs (case_id)",
        "CREATE INDEX ix_agent_runs_created_at ON agent_runs (created_at)",
        "CREATE INDEX ix_agent_runs_runtime_agent_id ON agent_runs (runtime_agent_id)",
        "CREATE INDEX ix_agent_runs_session_id ON agent_runs (session_id)",
        "CREATE INDEX ix_agent_runs_status ON agent_runs (status)",
        "CREATE UNIQUE INDEX ix_agent_runs_trace_id ON agent_runs (trace_id)",
        "CREATE INDEX ix_agent_runs_trace_status ON agent_runs (trace_status)",
        "CREATE INDEX ix_agent_runs_updated_at ON agent_runs (updated_at)",
        "CREATE UNIQUE INDEX ux_agent_runs_client_operation ON agent_runs (client_operation_id) WHERE client_operation_id IS NOT NULL",
        "CREATE UNIQUE INDEX ux_agent_runs_one_active_per_session ON agent_runs (session_id) WHERE status IN ('queued','running','waiting_human','waiting_external','finalizing')",
    )
    for statement in indexes:
        connection.execute(statement)
