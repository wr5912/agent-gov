from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction
from .state_machines import AGENT_DELETION_STATES, AGENT_DELETION_TRANSITIONS

_TABLE = "agent_deletion_operations"
_JOURNAL_FIXED_COLUMNS = (
    "operation_id",
    "idempotency_key",
    "agent_id",
    "agent_instance_etag",
    "workspace_path",
    "expected_device",
    "expected_inode",
    "expected_mount_id",
    "quarantine_path",
    "deleted_json",
    "impact_json",
    "created_at",
)
_TERMINAL_FIXED_COLUMNS = _JOURNAL_FIXED_COLUMNS + (
    "state",
    "quarantine_confirmed",
    "purge_confirmed",
    "completed_at",
)


def migrate_0059_agent_deletion_authority_hardening(
    connection: Connection,
) -> None:
    """Install append-only lifecycle authority on existing deletion journals."""

    begin_sqlite_write_transaction(connection)
    _create_enum_triggers(connection)
    _create_state_transition_trigger(connection)
    _create_authority_immutability_trigger(connection)
    _create_terminal_immutability_trigger(connection)
    _create_no_delete_trigger(connection)


def _create_enum_triggers(connection: Connection) -> None:
    allowed = _sql_values(AGENT_DELETION_STATES)
    for event, suffix in (("INSERT", "insert"), ("UPDATE OF state", "update_of_state")):
        trigger = f"ck_agent_deletion_operation_state_{suffix}"
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE {event} ON {_TABLE}
            FOR EACH ROW WHEN NEW.state NOT IN ({allowed})
            BEGIN
                SELECT RAISE(ABORT, 'invalid agent deletion operation state');
            END
            """
        )


def _create_state_transition_trigger(connection: Connection) -> None:
    trigger = "ck_agent_deletion_state_transition_update"
    allowed = " OR ".join(
        f"(OLD.state = {source!r} AND NEW.state IN ({_sql_values(targets)}))" for source, targets in sorted(AGENT_DELETION_TRANSITIONS.items()) if targets
    )
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE OF state ON {_TABLE}
        WHEN NEW.state != OLD.state AND NOT ({allowed})
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent deletion state transition');
        END
        """
    )


def _create_authority_immutability_trigger(connection: Connection) -> None:
    trigger = "ck_agent_deletion_authority_immutable_update"
    changed = _changed_expression(_JOURNAL_FIXED_COLUMNS)
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE OF {", ".join(_JOURNAL_FIXED_COLUMNS)} ON {_TABLE}
        WHEN {changed}
        BEGIN
            SELECT RAISE(ABORT, 'agent deletion journal authority is immutable');
        END
        """
    )


def _create_terminal_immutability_trigger(connection: Connection) -> None:
    trigger = "ck_agent_deletion_terminal_immutable_update"
    fixed = _same_expression(_TERMINAL_FIXED_COLUMNS)
    allowed_recovery = _terminal_recovery_expression()
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE ON {_TABLE}
        WHEN OLD.state = 'completed'
          AND NOT ({fixed} AND ({allowed_recovery}))
        BEGIN
            SELECT RAISE(ABORT, 'terminal agent deletion journal is immutable');
        END
        """
    )


def _terminal_recovery_expression() -> str:
    unchanged = (
        "NEW.witness_removed IS OLD.witness_removed "
        "AND NEW.error_json IS OLD.error_json "
        "AND NEW.attempt_count IS OLD.attempt_count "
        "AND NEW.updated_at IS OLD.updated_at"
    )
    witness_ack = "OLD.witness_removed = 0 AND NEW.witness_removed = 1 AND NEW.error_json IS OLD.error_json AND NEW.attempt_count IS OLD.attempt_count"
    cleanup_failure = (
        "OLD.witness_removed = 0 AND NEW.witness_removed = 0 "
        "AND NEW.attempt_count = OLD.attempt_count + 1 "
        "AND json_valid(NEW.error_json) "
        "AND json_type(NEW.error_json) = 'object' "
        "AND (SELECT COUNT(*) FROM json_each(NEW.error_json)) = 1 "
        "AND json_extract(NEW.error_json, '$.error_code') "
        "= 'AGENT_DELETION_WITNESS_CLEANUP_PENDING'"
    )
    return f"({unchanged}) OR ({witness_ack}) OR ({cleanup_failure})"


def _create_no_delete_trigger(connection: Connection) -> None:
    trigger = "ck_agent_deletion_no_delete"
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER {trigger}
        BEFORE DELETE ON {_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'agent deletion journal cannot be deleted');
        END
        """
    )


def _changed_expression(columns: tuple[str, ...]) -> str:
    return " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)


def _same_expression(columns: tuple[str, ...]) -> str:
    return " AND ".join(f"NEW.{column} IS OLD.{column}" for column in columns)


def _sql_values(values: set[str]) -> str:
    return ", ".join(repr(value) for value in sorted(values))
