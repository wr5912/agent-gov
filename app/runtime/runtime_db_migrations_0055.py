from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction
from .state_machines import (
    WORKSPACE_ACTIVATION_ACTIONS,
    WORKSPACE_ACTIVATION_RECOVERY_PHASES,
    WORKSPACE_ACTIVATION_STATES,
    WORKSPACE_ACTIVATION_TRANSITIONS,
)


def migrate_0055_workspace_activation_operations(connection: Connection) -> None:
    """Add the durable, per-Agent Workspace activation journal."""

    begin_sqlite_write_transaction(connection)
    _ensure_provision_outcome_token(connection)
    connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS agent_workspace_activation_operations (
            operation_id VARCHAR(128) NOT NULL PRIMARY KEY,
            import_id VARCHAR(128),
            agent_id VARCHAR(128) NOT NULL,
            action VARCHAR(32) NOT NULL,
            state VARCHAR(32) NOT NULL,
            original_head_sha VARCHAR(64) NOT NULL,
            base_commit_sha VARCHAR(64),
            candidate_commit_sha VARCHAR(64),
            candidate_tree_sha VARCHAR(64),
            target_commit_sha VARCHAR(64),
            snapshot_created BOOLEAN NOT NULL DEFAULT 0,
            original_status_text TEXT NOT NULL DEFAULT '',
            original_index_fingerprint VARCHAR(64) NOT NULL,
            original_workspace_fingerprint VARCHAR(64) NOT NULL,
            original_index_tree_sha VARCHAR(64),
            original_index_snapshot BLOB,
            recovery_phase VARCHAR(32) NOT NULL DEFAULT 'none',
            package_sha256 VARCHAR(64),
            tree_sha256 VARCHAR(64),
            suite_status VARCHAR(32),
            suite_json JSON NOT NULL DEFAULT '{{}}',
            diagnostics_json JSON NOT NULL DEFAULT '[]',
            maintenance_token VARCHAR(128) NOT NULL,
            maintenance_generation INTEGER NOT NULL,
            maintenance_expires_at VARCHAR(64) NOT NULL,
            error_json JSON NOT NULL DEFAULT '{{}}',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64)
            ,CONSTRAINT ck_workspace_activation_state CHECK (
                state IN ({_sql_values(WORKSPACE_ACTIVATION_STATES)})
            )
            ,CONSTRAINT ck_workspace_activation_action CHECK (
                action IN ({_sql_values(WORKSPACE_ACTIVATION_ACTIONS)})
            )
            ,CONSTRAINT ck_workspace_activation_recovery_phase CHECK (
                recovery_phase IN ({_sql_values(WORKSPACE_ACTIVATION_RECOVERY_PHASES)})
            )
        )
        """
    )
    _ensure_activation_columns(connection)
    refresh_0055_workspace_activation_authority(connection)
    _create_indexes(connection)


def refresh_0055_workspace_activation_authority(connection: Connection) -> None:
    """Reinstall the append-only activation authority on an existing table."""

    _create_enum_triggers(connection, "state", WORKSPACE_ACTIVATION_STATES)
    _create_enum_triggers(connection, "action", WORKSPACE_ACTIVATION_ACTIONS)
    _create_enum_triggers(connection, "recovery_phase", WORKSPACE_ACTIVATION_RECOVERY_PHASES)
    _create_state_transition_trigger(connection)
    _create_terminal_immutability_trigger(connection)
    _create_no_delete_trigger(connection)
    _create_graph_immutability_trigger(connection)


def _ensure_provision_outcome_token(connection: Connection) -> None:
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}
    if columns and "provision_completed_token" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN provision_completed_token VARCHAR(64)")


def _ensure_activation_columns(connection: Connection) -> None:
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_workspace_activation_operations)")}
    if "original_index_snapshot" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_workspace_activation_operations ADD COLUMN original_index_snapshot BLOB")
    if "recovery_phase" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_workspace_activation_operations ADD COLUMN recovery_phase VARCHAR(32) NOT NULL DEFAULT 'none'")


def _create_enum_triggers(connection: Connection, column: str, values: set[str]) -> None:
    valid = _sql_values(values)
    for event in ("INSERT", f"UPDATE OF {column}"):
        suffix = "insert" if event == "INSERT" else "update"
        trigger = f"ck_workspace_activation_{column}_{suffix}"
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger}
            BEFORE {event} ON agent_workspace_activation_operations
            WHEN NEW.{column} NOT IN ({valid})
            BEGIN
                SELECT RAISE(ABORT, 'invalid workspace activation {column}');
            END
            """
        )


def _sql_values(values: set[str]) -> str:
    return ", ".join(repr(value) for value in sorted(values))


def _create_state_transition_trigger(connection: Connection) -> None:
    trigger = "ck_workspace_activation_state_transition_update"
    allowed = " OR ".join(
        f"(OLD.state = {source!r} AND NEW.state IN ({_sql_values(targets)}))" for source, targets in sorted(WORKSPACE_ACTIVATION_TRANSITIONS.items()) if targets
    )
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE OF state ON agent_workspace_activation_operations
        WHEN NEW.state != OLD.state AND NOT ({allowed})
        BEGIN
            SELECT RAISE(ABORT, 'invalid workspace activation state transition');
        END
        """
    )


def _create_terminal_immutability_trigger(connection: Connection) -> None:
    trigger = "ck_workspace_activation_terminal_immutable_update"
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE ON agent_workspace_activation_operations
        WHEN OLD.state IN ('completed', 'rejected')
        BEGIN
            SELECT RAISE(ABORT, 'terminal workspace activation is immutable');
        END
        """
    )


def _create_no_delete_trigger(connection: Connection) -> None:
    trigger = "ck_workspace_activation_no_delete"
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE DELETE ON agent_workspace_activation_operations
        BEGIN
            SELECT RAISE(ABORT, 'workspace activation journal cannot be deleted');
        END
        """
    )


def _create_graph_immutability_trigger(connection: Connection) -> None:
    trigger = "ck_workspace_activation_graph_immutable_update"
    fields = (
        "import_id",
        "agent_id",
        "action",
        "original_head_sha",
        "base_commit_sha",
        "candidate_commit_sha",
        "candidate_tree_sha",
        "target_commit_sha",
        "snapshot_created",
        "original_status_text",
        "original_index_fingerprint",
        "original_workspace_fingerprint",
        "original_index_tree_sha",
        "original_index_snapshot",
        "package_sha256",
        "tree_sha256",
        "suite_status",
        "suite_json",
        "diagnostics_json",
        "maintenance_token",
        "maintenance_generation",
        "maintenance_expires_at",
    )
    changed = " OR ".join(f"NEW.{field} IS NOT OLD.{field}" for field in fields)
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE OF {", ".join(fields)} ON agent_workspace_activation_operations
        WHEN OLD.state != 'preparing' AND ({changed})
        BEGIN
            SELECT RAISE(ABORT, 'workspace activation graph journal is immutable');
        END
        """
    )


def _create_indexes(connection: Connection) -> None:
    connection.exec_driver_sql("DROP INDEX IF EXISTS ux_agent_workspace_activation_operations_fence")
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_import_id ON agent_workspace_activation_operations (import_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_agent_id ON agent_workspace_activation_operations (agent_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_action ON agent_workspace_activation_operations (action)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_state ON agent_workspace_activation_operations (state)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_candidate_commit_sha "
        "ON agent_workspace_activation_operations (candidate_commit_sha)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_maintenance_token ON agent_workspace_activation_operations (maintenance_token)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_maintenance_expires_at "
        "ON agent_workspace_activation_operations (maintenance_expires_at)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_created_at ON agent_workspace_activation_operations (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_operations_updated_at ON agent_workspace_activation_operations (updated_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_workspace_activation_operations_import "
        "ON agent_workspace_activation_operations (import_id) WHERE import_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_workspace_activation_operations_fence "
        "ON agent_workspace_activation_operations (agent_id) "
        "WHERE state IN ('preparing', 'prepared', 'completing', 'rejecting', 'recovery_required')",
    )
    for statement in statements:
        connection.exec_driver_sql(statement)
