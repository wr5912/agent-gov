from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction
from .state_machines import AGENT_DELETION_STATES


def migrate_0056_agent_deletion_operations(connection: Connection) -> None:
    """Add the durable business-Agent deletion cleanup journal."""

    begin_sqlite_write_transaction(connection)
    _backfill_agent_instance_tokens(connection)
    connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS agent_deletion_operations (
            operation_id VARCHAR(128) NOT NULL PRIMARY KEY,
            idempotency_key VARCHAR(256) NOT NULL,
            agent_id VARCHAR(128) NOT NULL,
            agent_instance_etag VARCHAR(128) NOT NULL,
            state VARCHAR(32) NOT NULL,
            workspace_path VARCHAR(2048) NOT NULL,
            expected_device INTEGER,
            expected_inode INTEGER,
            expected_mount_id INTEGER,
            quarantine_path VARCHAR(2048) NOT NULL,
            quarantine_confirmed BOOLEAN NOT NULL DEFAULT 0,
            purge_confirmed BOOLEAN NOT NULL DEFAULT 0,
            witness_removed BOOLEAN NOT NULL DEFAULT 0,
            deleted_json JSON NOT NULL DEFAULT '{{}}',
            impact_json JSON NOT NULL DEFAULT '{{}}',
            error_json JSON NOT NULL DEFAULT '{{}}',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64),
            CONSTRAINT ck_agent_deletion_operation_state
                CHECK (state IN ({_sql_values(AGENT_DELETION_STATES)})),
            CONSTRAINT ck_agent_deletion_purge_requires_quarantine
                CHECK (NOT purge_confirmed OR quarantine_confirmed),
            CONSTRAINT ck_agent_deletion_completed_cleanup
                CHECK (state != 'completed' OR (quarantine_confirmed AND purge_confirmed)),
            CONSTRAINT ck_agent_deletion_witness_terminal
                CHECK (NOT witness_removed OR state = 'completed')
        )
        """
    )
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_deletion_operations_idempotency ON agent_deletion_operations (idempotency_key)")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_deletion_operations_agent_instance ON agent_deletion_operations (agent_id, agent_instance_etag)"
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_deletion_operations_pending_agent ON agent_deletion_operations (agent_id) WHERE state = 'cleanup_pending'"
    )
    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_agent_deletion_operations_state_created")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_deletion_operations_state_updated ON agent_deletion_operations (state, updated_at, created_at, operation_id)"
    )
    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_agent_deletion_operations_witness_cleanup")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_deletion_operations_witness_cleanup "
        "ON agent_deletion_operations (state, witness_removed, updated_at, completed_at, operation_id) "
        "WHERE state = 'completed' AND witness_removed = 0"
    )
    _create_state_triggers(connection)


def _backfill_agent_instance_tokens(connection: Connection) -> None:
    registry_columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}
    if not registry_columns:
        return
    if "provision_completed_token" not in registry_columns:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN provision_completed_token VARCHAR(64)")
    quarantine_exclusion = ""
    if {"deleted_at", "provision_previous_json"} <= registry_columns:
        quarantine_exclusion = """
          AND NOT (
              deleted_at IS NOT NULL
              AND COALESCE(CASE
                  WHEN json_valid(provision_previous_json)
                  THEN json_extract(provision_previous_json, '$.kind')
                  ELSE NULL
              END, '') = 'workspace_must_be_absent'
          )
        """
    connection.exec_driver_sql(
        f"""
        UPDATE agent_registry
        SET provision_completed_token = lower(hex(randomblob(16)))
        WHERE COALESCE(provision_state, 'ready') = 'ready'
          AND (provision_completed_token IS NULL OR provision_completed_token = '')
          {quarantine_exclusion}
        """
    )


def _create_state_triggers(connection: Connection) -> None:
    for action in ("INSERT", "UPDATE OF state"):
        suffix = action.lower().replace(" ", "_")
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER IF NOT EXISTS ck_agent_deletion_operation_state_{suffix}
            BEFORE {action} ON agent_deletion_operations
            FOR EACH ROW WHEN NEW.state NOT IN ({_sql_values(AGENT_DELETION_STATES)})
            BEGIN
                SELECT RAISE(ABORT, 'invalid agent deletion operation state');
            END
            """
        )


def _sql_values(values: set[str]) -> str:
    return ", ".join(repr(value) for value in sorted(values))
