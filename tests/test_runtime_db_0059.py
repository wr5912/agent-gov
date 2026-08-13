from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.runtime_db import (
    SchemaMigration,
    make_engine,
    make_session_factory,
)
from app.runtime.runtime_db_migrations_0056 import (
    migrate_0056_agent_deletion_operations,
)
from app.runtime.runtime_db_migrations_0059 import (
    migrate_0059_agent_deletion_authority_hardening,
)
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from tests.business_agent_deletion_saga_test_support import (
    build_deletion_saga_harness,
    complete_without_witness_cleanup,
)

_MIGRATION = "0059_agent_deletion_authority_hardening"
_AUTHORITY_TRIGGERS = {
    "ck_agent_deletion_operation_state_insert",
    "ck_agent_deletion_operation_state_update_of_state",
    "ck_agent_deletion_state_transition_update",
    "ck_agent_deletion_authority_immutable_update",
    "ck_agent_deletion_terminal_immutable_update",
    "ck_agent_deletion_no_delete",
}


def test_0059_fresh_install_is_idempotent_and_preserves_legal_recovery() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        migrate_0056_agent_deletion_operations(connection)
        migrate_0059_agent_deletion_authority_hardening(connection)
        migrate_0059_agent_deletion_authority_hardening(connection)
        _assert_authority_installed(connection)
        _insert_operation(connection, "fresh")

        _complete_operation(connection, "fresh")
        _assert_illegal_terminal_mutations_fail(connection, "fresh")
        _record_witness_failure(connection, "fresh")
        _confirm_witness_removed(connection, "fresh")
        _assert_confirmed_witness_replay_is_strict_no_op(connection, "fresh")
        row = connection.exec_driver_sql("SELECT state, witness_removed, attempt_count FROM agent_deletion_operations WHERE operation_id = 'fresh'").one()

    assert tuple(row) == ("completed", 1, 2)


def test_0059_service_first_witness_ack_and_replay_remain_compatible(
    tmp_path: Path,
) -> None:
    harness = build_deletion_saga_harness(tmp_path)
    engine = create_engine(
        f"sqlite:///{harness.data_dir / 'runtime.sqlite3'}",
        future=True,
    )
    with engine.begin() as connection:
        migrate_0059_agent_deletion_authority_hardening(connection)

    complete_without_witness_cleanup(harness, key="service-witness")
    first = harness.delete(key="service-witness")
    with engine.connect() as connection:
        before_replay = _operation_snapshot(connection, first.operation_id)

    replay = harness.delete(key="service-witness")
    with engine.connect() as connection:
        after_replay = _operation_snapshot(connection, first.operation_id)

    assert first.witness_removed is True
    assert replay == first
    assert after_replay == before_replay


def test_0059_upgrades_a_0058_snapshot_without_rewriting_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime-0058.sqlite3"
    make_session_factory(db_path)
    with make_engine(db_path).begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM schema_migrations WHERE version = ?",
            (_MIGRATION,),
        )
        for trigger in _AUTHORITY_TRIGGERS:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
        migrate_0056_agent_deletion_operations(connection)
        _insert_operation(connection, "historical", completed=True)
        connection.exec_driver_sql("UPDATE agent_deletion_operations SET state = 'cleanup_pending' WHERE operation_id = 'historical'")
        connection.exec_driver_sql("UPDATE agent_deletion_operations SET state = 'completed' WHERE operation_id = 'historical'")

    upgraded_factory = make_session_factory(db_path)
    make_session_factory(db_path)

    with upgraded_factory() as db:
        assert db.get(SchemaMigration, "0058_workspace_activation_authority_hardening") is not None
        assert db.get(SchemaMigration, _MIGRATION) is not None
    with make_engine(db_path).begin() as connection:
        _assert_authority_installed(connection)
        row = connection.exec_driver_sql("SELECT state, deleted_json, completed_at FROM agent_deletion_operations WHERE operation_id = 'historical'").one()
        assert tuple(row) == ("completed", '{"agent_id":"historical"}', "completed")
        _assert_illegal_terminal_mutations_fail(connection, "historical")
        _record_witness_failure(connection, "historical")


def _insert_operation(
    connection: Connection,
    operation_id: str,
    *,
    completed: bool = False,
) -> None:
    state = "completed" if completed else "cleanup_pending"
    confirmed = 1 if completed else 0
    completed_at = "completed" if completed else None
    connection.exec_driver_sql(
        """
        INSERT INTO agent_deletion_operations (
            operation_id, idempotency_key, agent_id, agent_instance_etag,
            state, workspace_path, quarantine_path, quarantine_confirmed,
            purge_confirmed, deleted_json, impact_json, error_json,
            attempt_count, created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', 0, 'created', ?, ?)
        """,
        (
            operation_id,
            f"key-{operation_id}",
            f"agent-{operation_id}",
            f"etag-{operation_id}",
            state,
            f"/data/business-agents/{operation_id}",
            f"/data/.agent-deletion-quarantine/{operation_id}",
            confirmed,
            confirmed,
            f'{{"agent_id":"{operation_id}"}}',
            completed_at or "created",
            completed_at,
        ),
    )


def _complete_operation(connection: Connection, operation_id: str) -> None:
    connection.exec_driver_sql(
        """
        UPDATE agent_deletion_operations
        SET quarantine_confirmed = 1, purge_confirmed = 1,
            state = 'completed', attempt_count = attempt_count + 1,
            error_json = '{}', updated_at = 'completed',
            completed_at = 'completed'
        WHERE operation_id = ?
        """,
        (operation_id,),
    )


def _record_witness_failure(connection: Connection, operation_id: str) -> None:
    connection.exec_driver_sql(
        """
        UPDATE agent_deletion_operations
        SET attempt_count = attempt_count + 1,
            error_json = '{"error_code":"AGENT_DELETION_WITNESS_CLEANUP_PENDING"}',
            updated_at = 'witness-retry'
        WHERE operation_id = ?
        """,
        (operation_id,),
    )


def _confirm_witness_removed(connection: Connection, operation_id: str) -> None:
    connection.exec_driver_sql(
        """
        UPDATE agent_deletion_operations
        SET witness_removed = 1, updated_at = 'witness-removed'
        WHERE operation_id = ?
        """,
        (operation_id,),
    )


def _assert_confirmed_witness_replay_is_strict_no_op(
    connection: Connection,
    operation_id: str,
) -> None:
    before_replay = _operation_snapshot(connection, operation_id)
    with pytest.raises(IntegrityError):
        connection.exec_driver_sql(
            """
            UPDATE agent_deletion_operations
            SET witness_removed = 1, updated_at = 'replayed'
            WHERE operation_id = ?
            """,
            (operation_id,),
        )
    connection.exec_driver_sql(
        """
        UPDATE agent_deletion_operations
        SET witness_removed = witness_removed,
            error_json = error_json,
            attempt_count = attempt_count,
            updated_at = updated_at
        WHERE operation_id = ?
        """,
        (operation_id,),
    )
    assert _operation_snapshot(connection, operation_id) == before_replay


def _operation_snapshot(
    connection: Connection,
    operation_id: str,
) -> tuple[object, ...]:
    row = connection.exec_driver_sql(
        "SELECT * FROM agent_deletion_operations WHERE operation_id = ?",
        (operation_id,),
    ).one()
    return tuple(row)


def _assert_authority_installed(connection: Connection) -> None:
    triggers = {
        str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'agent_deletion_operations'")
    }
    assert triggers >= _AUTHORITY_TRIGGERS


def _assert_illegal_terminal_mutations_fail(
    connection: Connection,
    operation_id: str,
) -> None:
    statements = (
        "UPDATE agent_deletion_operations SET state = 'cleanup_pending' WHERE operation_id = ?",
        "UPDATE agent_deletion_operations SET deleted_json = '{\"tampered\":true}' WHERE operation_id = ?",
        "UPDATE agent_deletion_operations SET error_json = '{\"tampered\":true}' WHERE operation_id = ?",
        "DELETE FROM agent_deletion_operations WHERE operation_id = ?",
    )
    for statement in statements:
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(statement, (operation_id,))
