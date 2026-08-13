from __future__ import annotations

from sqlalchemy.engine import Connection

from app.runtime.runtime_db_base import begin_sqlite_write_transaction
from app.runtime.workspace_activation_recovery import (
    RECOVERY_ACTIONS,
    RECOVERY_ATTEMPT_STATES,
    RECOVERY_REPAIRABLE_REF_NAMES,
    RECOVERY_TERMINAL_ACTIVATION_STATES,
)


def migrate_0057_workspace_activation_operator_recovery(connection: Connection) -> None:
    """Add the append-only operator recovery attempt journal."""

    begin_sqlite_write_transaction(connection)
    connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS agent_workspace_activation_recovery_attempts (
            recovery_id VARCHAR(128) NOT NULL PRIMARY KEY,
            operation_id VARCHAR(128) NOT NULL,
            agent_id VARCHAR(128) NOT NULL,
            action VARCHAR(32) NOT NULL,
            state VARCHAR(32) NOT NULL,
            requested_state_digest VARCHAR(80) NOT NULL,
            observed_state_digest VARCHAR(80),
            observed_context_digest VARCHAR(80),
            operator VARCHAR(128) NOT NULL,
            reason TEXT NOT NULL,
            result_json JSON NOT NULL DEFAULT '{{}}',
            error_json JSON NOT NULL DEFAULT '{{}}',
            created_at VARCHAR(64) NOT NULL,
            started_at VARCHAR(64),
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64),
            CONSTRAINT fk_workspace_activation_recovery_operation
                FOREIGN KEY(operation_id)
                REFERENCES agent_workspace_activation_operations(operation_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_workspace_activation_recovery_attempt_action
                CHECK (action IN ({_sql_values(RECOVERY_ACTIONS)})),
            CONSTRAINT ck_workspace_activation_recovery_attempt_state
                CHECK (state IN ({_sql_values(RECOVERY_ATTEMPT_STATES)})),
            CONSTRAINT ck_workspace_activation_recovery_attempt_terminal_time
                CHECK (
                    (state = 'reserved' AND completed_at IS NULL)
                    OR
                    (state IN ('completed', 'failed') AND completed_at IS NOT NULL)
                ),
            CONSTRAINT ck_workspace_activation_recovery_completed_started
                CHECK (state != 'completed' OR observed_state_digest IS NOT NULL)
        )
        """
    )
    _create_indexes(connection)
    refresh_0057_workspace_activation_recovery_authority(connection)


def refresh_0057_workspace_activation_recovery_authority(
    connection: Connection,
) -> None:
    """Reinstall recovery attempt constraints on an existing table."""

    _create_enum_triggers(connection)
    _create_append_only_triggers(connection)


def _create_indexes(connection: Connection) -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_recovery_attempts_operation_id "
        "ON agent_workspace_activation_recovery_attempts (operation_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_recovery_attempts_agent_id ON agent_workspace_activation_recovery_attempts (agent_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_recovery_attempts_action ON agent_workspace_activation_recovery_attempts (action)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_recovery_attempts_state ON agent_workspace_activation_recovery_attempts (state)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_recovery_attempts_created_at ON agent_workspace_activation_recovery_attempts (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_agent_workspace_activation_recovery_attempts_updated_at ON agent_workspace_activation_recovery_attempts (updated_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_activation_recovery_active_operation "
        "ON agent_workspace_activation_recovery_attempts (operation_id) "
        "WHERE state = 'reserved'",
        "CREATE INDEX IF NOT EXISTS ix_workspace_activation_recovery_operation_created "
        "ON agent_workspace_activation_recovery_attempts "
        "(operation_id, created_at, recovery_id)",
    )
    for statement in statements:
        connection.exec_driver_sql(statement)


def _create_enum_triggers(connection: Connection) -> None:
    for column, values in (
        ("action", RECOVERY_ACTIONS),
        ("state", RECOVERY_ATTEMPT_STATES),
    ):
        allowed = _sql_values(values)
        for event in ("INSERT", f"UPDATE OF {column}"):
            suffix = "insert" if event == "INSERT" else "update"
            name = f"ck_workspace_activation_recovery_{column}_{suffix}"
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER {name}
                BEFORE {event} ON agent_workspace_activation_recovery_attempts
                WHEN NEW.{column} NOT IN ({allowed})
                BEGIN
                    SELECT RAISE(ABORT, 'invalid workspace activation recovery {column}');
                END
                """
            )


def _create_append_only_triggers(connection: Connection) -> None:
    for name in (
        "ck_workspace_activation_recovery_insert_shape",
        "ck_workspace_activation_recovery_identity_immutable",
        "ck_workspace_activation_recovery_terminal_immutable",
        "ck_workspace_activation_recovery_reserved_update_shape",
        "ck_workspace_activation_recovery_terminal_evidence",
        "ck_workspace_activation_recovery_state_transition",
        "ck_workspace_activation_recovery_no_delete",
    ):
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    _create_insert_shape_trigger(connection)
    _create_identity_terminal_triggers(connection)
    _create_reserved_update_trigger(connection)
    _create_terminal_evidence_trigger(connection)
    _create_state_delete_triggers(connection)


def _create_insert_shape_trigger(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TRIGGER ck_workspace_activation_recovery_insert_shape
        BEFORE INSERT ON agent_workspace_activation_recovery_attempts
        WHEN NEW.state != 'reserved'
          OR NEW.observed_state_digest IS NOT NULL
          OR NEW.observed_context_digest IS NOT NULL
          OR NEW.started_at IS NOT NULL
          OR json(NEW.result_json) != '{}'
          OR json(NEW.error_json) != '{}'
          OR NEW.completed_at IS NOT NULL
          OR NEW.updated_at IS NOT NEW.created_at
        BEGIN
            SELECT RAISE(ABORT, 'workspace activation recovery insert must be pristine reserved');
        END
        """
    )


def _create_identity_terminal_triggers(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ck_workspace_activation_recovery_identity_immutable
        BEFORE UPDATE ON agent_workspace_activation_recovery_attempts
        WHEN NEW.recovery_id IS NOT OLD.recovery_id
          OR NEW.operation_id IS NOT OLD.operation_id
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.action IS NOT OLD.action
          OR NEW.requested_state_digest IS NOT OLD.requested_state_digest
          OR NEW.operator IS NOT OLD.operator
          OR NEW.reason IS NOT OLD.reason
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'workspace activation recovery identity is immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER ck_workspace_activation_recovery_terminal_immutable
        BEFORE UPDATE ON agent_workspace_activation_recovery_attempts
        WHEN OLD.state IN ('completed', 'failed')
        BEGIN
            SELECT RAISE(ABORT, 'terminal workspace activation recovery attempt is immutable');
        END
        """
    )


def _create_reserved_update_trigger(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TRIGGER ck_workspace_activation_recovery_reserved_update_shape
        BEFORE UPDATE ON agent_workspace_activation_recovery_attempts
        WHEN OLD.state = 'reserved'
         AND NOT (
            (
                NEW.state = 'reserved'
                AND OLD.observed_state_digest IS NULL
                AND OLD.observed_context_digest IS NULL
                AND OLD.started_at IS NULL
                AND NEW.observed_state_digest IS NOT NULL
                AND NEW.observed_context_digest IS NOT NULL
                AND NEW.started_at IS NOT NULL
                AND NEW.result_json IS OLD.result_json
                AND NEW.error_json IS OLD.error_json
                AND NEW.completed_at IS OLD.completed_at
            )
            OR
            (
                NEW.state IN ('completed', 'failed')
                AND NEW.observed_state_digest IS OLD.observed_state_digest
                AND NEW.observed_context_digest IS OLD.observed_context_digest
                AND NEW.started_at IS OLD.started_at
                AND NEW.completed_at IS NOT NULL
                AND (
                    (
                        NEW.state = 'completed'
                        AND NEW.observed_state_digest IS NOT NULL
                        AND NEW.observed_context_digest IS NOT NULL
                        AND NEW.started_at IS NOT NULL
                        AND NEW.error_json IS OLD.error_json
                    )
                    OR
                    (
                        NEW.state = 'failed'
                        AND NEW.result_json IS OLD.result_json
                    )
                )
            )
         )
        BEGIN
            SELECT RAISE(ABORT, 'invalid reserved workspace activation recovery update');
        END
        """
    )


def _create_terminal_evidence_trigger(connection: Connection) -> None:
    terminal_states = _sql_values(RECOVERY_TERMINAL_ACTIVATION_STATES)
    repaired_refs = _sql_values(RECOVERY_REPAIRABLE_REF_NAMES)
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER ck_workspace_activation_recovery_terminal_evidence
        BEFORE UPDATE ON agent_workspace_activation_recovery_attempts
        WHEN NEW.state IN ('completed', 'failed')
         AND COALESCE(
            CASE NEW.state
                WHEN 'completed' THEN (
                    json_type(NEW.result_json) = 'object'
                    AND (SELECT COUNT(*) FROM json_each(NEW.result_json)) = 4
                    AND json_type(NEW.result_json, '$.activation_state') = 'text'
                    AND json_extract(NEW.result_json, '$.activation_state') IN ({terminal_states})
                    AND json_type(NEW.result_json, '$.resolution') = 'text'
                    AND json_extract(NEW.result_json, '$.resolution')
                        = json_extract(NEW.result_json, '$.activation_state')
                    AND json_type(NEW.result_json, '$.already_applied') IN ('true', 'false')
                    AND json_type(NEW.result_json, '$.repaired_ref_names') = 'array'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM json_each(NEW.result_json, '$.repaired_ref_names') AS repaired
                        WHERE repaired.type != 'text'
                           OR repaired.value NOT IN ({repaired_refs})
                    )
                    AND (
                        SELECT COUNT(*)
                        FROM json_each(NEW.result_json, '$.repaired_ref_names')
                    ) = (
                        SELECT COUNT(DISTINCT repaired.value)
                        FROM json_each(NEW.result_json, '$.repaired_ref_names') AS repaired
                    )
                    AND json(NEW.error_json) = '{{}}'
                )
                WHEN 'failed' THEN (
                    json(NEW.result_json) = '{{}}'
                    AND json_type(NEW.error_json) = 'object'
                    AND (SELECT COUNT(*) FROM json_each(NEW.error_json)) = 1
                    AND json_type(NEW.error_json, '$.code') = 'text'
                    AND length(json_extract(NEW.error_json, '$.code')) BETWEEN 1 AND 96
                    AND json_extract(NEW.error_json, '$.code')
                        = trim(json_extract(NEW.error_json, '$.code'))
                    AND json_extract(NEW.error_json, '$.code')
                        NOT GLOB '*[^A-Z0-9_]*'
                )
                ELSE 0
            END,
            0
         ) != 1
        BEGIN
            SELECT RAISE(ABORT, 'invalid workspace activation recovery terminal evidence');
        END
        """
    )


def _create_state_delete_triggers(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TRIGGER ck_workspace_activation_recovery_state_transition
        BEFORE UPDATE OF state ON agent_workspace_activation_recovery_attempts
        WHEN NEW.state != OLD.state
         AND (OLD.state != 'reserved' OR NEW.state NOT IN ('completed', 'failed'))
        BEGIN
            SELECT RAISE(ABORT, 'invalid workspace activation recovery transition');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER ck_workspace_activation_recovery_no_delete
        BEFORE DELETE ON agent_workspace_activation_recovery_attempts
        BEGIN
            SELECT RAISE(ABORT, 'workspace activation recovery attempts are append-only');
        END
        """
    )


def _sql_values(values: set[str]) -> str:
    return ", ".join(repr(value) for value in sorted(values))
