from __future__ import annotations

import pytest
from app.runtime.agent_deletion_db import AgentDeletionOperationModel
from app.runtime.runtime_db_migrations_0056 import migrate_0056_agent_deletion_operations
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError


def test_0056_orm_clean_and_historical_upgrade_have_exact_schema_parity() -> None:
    clean = create_engine("sqlite+pysqlite:///:memory:", future=True)
    upgrade = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with clean.begin() as connection:
        AgentDeletionOperationModel.__table__.create(connection)
        migrate_0056_agent_deletion_operations(connection)
        migrate_0056_agent_deletion_operations(connection)
        clean_projection = _schema_projection(connection)
    with upgrade.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE agent_registry (agent_id VARCHAR(128) PRIMARY KEY, provision_state VARCHAR(32), provision_completed_token VARCHAR(64))"
        )
        migrate_0056_agent_deletion_operations(connection)
        migrate_0056_agent_deletion_operations(connection)
        upgrade_projection = _schema_projection(connection)

    assert clean_projection == upgrade_projection


def test_0056_creates_deletion_journal_idempotently_and_preserves_runtime_data() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE historical_runtime_row (row_id VARCHAR(32) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO historical_runtime_row VALUES ('preserved')")
        connection.exec_driver_sql(
            "CREATE TABLE agent_registry (agent_id VARCHAR(128) PRIMARY KEY, provision_state VARCHAR(32), provision_completed_token VARCHAR(64))"
        )
        connection.exec_driver_sql("INSERT INTO agent_registry VALUES ('legacy-ready', 'ready', NULL)")

        migrate_0056_agent_deletion_operations(connection)
        migrate_0056_agent_deletion_operations(connection)

        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_deletion_operations)")}
        assert {
            "operation_id",
            "idempotency_key",
            "agent_id",
            "agent_instance_etag",
            "state",
            "workspace_path",
            "expected_device",
            "expected_inode",
            "expected_mount_id",
            "quarantine_path",
            "quarantine_confirmed",
            "purge_confirmed",
            "witness_removed",
            "deleted_json",
            "impact_json",
            "error_json",
            "attempt_count",
            "created_at",
            "updated_at",
            "completed_at",
        } <= columns
        indexes = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA index_list(agent_deletion_operations)")}
        assert "ux_agent_deletion_operations_idempotency" in indexes
        assert "ux_agent_deletion_operations_pending_agent" in indexes
        assert connection.exec_driver_sql("SELECT row_id FROM historical_runtime_row").scalar_one() == "preserved"
        instance_token = connection.exec_driver_sql("SELECT provision_completed_token FROM agent_registry WHERE agent_id = 'legacy-ready'").scalar_one()
        assert isinstance(instance_token, str) and len(instance_token) == 32


def test_0056_backfills_public_instances_but_preserves_never_public_quarantine() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_registry (
                agent_id VARCHAR(128) PRIMARY KEY,
                provision_state VARCHAR(32),
                provision_completed_token VARCHAR(64),
                deleted_at VARCHAR(64),
                provision_previous_json JSON
            )
            """
        )
        connection.exec_driver_sql("INSERT INTO agent_registry VALUES ('active', 'ready', NULL, NULL, NULL)")
        connection.exec_driver_sql("INSERT INTO agent_registry VALUES ('user-deleted', 'ready', NULL, 'now', NULL)")
        connection.exec_driver_sql(
            "INSERT INTO agent_registry VALUES ('never-public', 'ready', NULL, 'now', ?)",
            ('{"kind":"workspace_must_be_absent","workspace_dir":"/data/business-agents/never-public/workspace"}',),
        )

        migrate_0056_agent_deletion_operations(connection)
        migrate_0056_agent_deletion_operations(connection)

        rows = dict(connection.exec_driver_sql("SELECT agent_id, provision_completed_token FROM agent_registry ORDER BY agent_id").all())
        assert isinstance(rows["active"], str) and len(rows["active"]) == 32
        assert isinstance(rows["user-deleted"], str) and len(rows["user-deleted"]) == 32
        assert rows["never-public"] is None


def test_0056_enforces_state_idempotency_and_single_pending_agent_constraints() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    values = "?, ?, ?, ?, ?, '/data/business-agents/a', 1, 2, 3, '/data/.agent-deletion-quarantine/op', 0, 0, 0, '{}', '{}', '{}', 0, 'now', 'now', NULL"
    with engine.begin() as connection:
        migrate_0056_agent_deletion_operations(connection)
        connection.exec_driver_sql(
            f"INSERT INTO agent_deletion_operations VALUES ({values})",
            ("op-1", "key-1", "a", "instance-1", "cleanup_pending"),
        )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                f"INSERT INTO agent_deletion_operations VALUES ({values})",
                ("op-2", "key-1", "b", "instance-2", "cleanup_pending"),
            )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                f"INSERT INTO agent_deletion_operations VALUES ({values})",
                ("op-3", "key-3", "a", "instance-1", "cleanup_pending"),
            )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                f"INSERT INTO agent_deletion_operations VALUES ({values})",
                ("op-4", "key-4", "c", "instance-4", "unknown"),
            )

        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE agent_deletion_operations SET purge_confirmed = 1 WHERE operation_id = 'op-1'")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE agent_deletion_operations SET state = 'completed' WHERE operation_id = 'op-1'")
        connection.exec_driver_sql(
            "UPDATE agent_deletion_operations SET quarantine_confirmed = 1, purge_confirmed = 1, state = 'completed' WHERE operation_id = 'op-1'"
        )
        connection.exec_driver_sql(
            f"INSERT INTO agent_deletion_operations VALUES ({values})",
            ("op-5", "key-5", "a", "instance-2", "cleanup_pending"),
        )


def _schema_projection(connection) -> tuple[object, object, object]:  # type: ignore[no-untyped-def]
    columns = tuple(
        (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5])) for row in connection.exec_driver_sql("PRAGMA table_info(agent_deletion_operations)")
    )
    indexes: list[tuple[object, ...]] = []
    for row in connection.exec_driver_sql("PRAGMA index_list(agent_deletion_operations)"):
        name = str(row[1])
        if name.startswith("sqlite_autoindex"):
            continue
        columns_for_index = tuple(str(item[2]) for item in connection.exec_driver_sql(f'PRAGMA index_info("{name}")'))
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).scalar_one()
        predicate = ""
        if isinstance(sql, str) and " WHERE " in sql.upper():
            predicate = " ".join(sql[sql.upper().index(" WHERE ") + 7 :].split()).lower()
        indexes.append((name, int(row[2]), int(row[4]), columns_for_index, predicate))
    triggers = tuple(
        (str(row[0]), " ".join(str(row[1]).split()).lower())
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'agent_deletion_operations' ORDER BY name"
        )
    )
    return columns, tuple(sorted(indexes)), triggers
