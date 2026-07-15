from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction, utc_now
from .runtime_db_migrations_0036_feedback_cases import migrate_feedback_case_sources


def migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection: Connection) -> None:
    """Add durable admission, recovery, ownership, datasets, and SDK turn reconciliation."""
    begin_sqlite_write_transaction(connection)
    _migrate_session_reconciliation(connection)
    migrate_feedback_case_sources(connection)
    _migrate_test_datasets(connection)


def _migrate_session_reconciliation(connection: Connection) -> None:
    session_columns = _table_columns(connection, "sessions")
    for column_name, ddl in {
        "active_run_generation": "INTEGER NOT NULL DEFAULT 0",
        "sdk_project_key": "VARCHAR(256)",
        "sdk_store_ready_at": "VARCHAR(64)",
        "sdk_store_migration_error": "TEXT",
    }.items():
        if session_columns and column_name not in session_columns:
            connection.exec_driver_sql(f"ALTER TABLE sessions ADD COLUMN {column_name} {ddl}")


def _create_column_indexes(
    connection: Connection,
    table_name: str,
    columns: tuple[str, ...],
) -> None:
    for column_name in columns:
        connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_{column_name} ON {table_name} ({column_name})")


def _migrate_test_datasets(connection: Connection) -> None:
    _migrate_test_dataset_owner_columns(connection)
    _backfill_test_dataset_revisions(connection)
    _migrate_eval_run_dataset_reference(connection)
    _archive_legacy_test_dataset_assets(connection)


def _migrate_test_dataset_owner_columns(connection: Connection) -> None:
    columns = _table_columns(connection, "test_datasets")
    if not columns:
        return
    if "owner_kind" not in columns:
        connection.exec_driver_sql("ALTER TABLE test_datasets ADD COLUMN owner_kind VARCHAR(32) NOT NULL DEFAULT 'business_agent'")
    if "owner_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE test_datasets ADD COLUMN owner_id VARCHAR(128) NOT NULL DEFAULT ''")
    connection.exec_driver_sql("UPDATE test_datasets SET owner_id = agent_id WHERE owner_id IS NULL OR owner_id = ''")
    _create_column_indexes(
        connection,
        "test_datasets",
        (
            "agent_id",
            "owner_kind",
            "owner_id",
            "source_improvement_id",
            "lifecycle_state",
            "created_at",
            "updated_at",
        ),
    )


def _backfill_test_dataset_revisions(connection: Connection) -> None:
    if not _table_columns(connection, "test_datasets") or not _table_columns(connection, "test_dataset_revisions"):
        return
    _create_column_indexes(
        connection,
        "test_dataset_revisions",
        ("dataset_id", "created_at"),
    )
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_test_dataset_revisions_dataset_revision ON test_dataset_revisions (dataset_id, revision)")
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO test_dataset_revisions (
            revision_id, dataset_id, revision, previous_lifecycle_state, lifecycle_state,
            operator, reason, before_json, after_json, created_at
        )
        SELECT dataset_id || ':revision:' || revision, dataset_id, revision, NULL,
               lifecycle_state, 'migration', '0036 existing dataset revision backfill',
               '{}', json_object(
                   'dataset_id', dataset_id,
                   'agent_id', agent_id,
                   'owner_kind', owner_kind,
                   'owner_id', owner_id,
                   'revision', revision,
                   'lifecycle_state', lifecycle_state
               ), created_at
        FROM test_datasets
        """
    )


def _migrate_eval_run_dataset_reference(connection: Connection) -> None:
    columns = _table_columns(connection, "eval_runs")
    if columns and "dataset_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE eval_runs ADD COLUMN dataset_id VARCHAR(128)")
    if columns:
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_runs_dataset_id ON eval_runs (dataset_id)")


def _archive_legacy_test_dataset_assets(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS archived_test_dataset_assets (
            legacy_asset_id VARCHAR(128) NOT NULL PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            title VARCHAR(512) NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            source_improvement_id VARCHAR(128) NOT NULL DEFAULT '',
            inherited_from VARCHAR(128) NOT NULL DEFAULT '',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            archived_at VARCHAR(64) NOT NULL,
            reason VARCHAR(512) NOT NULL
        )
        """
    )
    _create_column_indexes(
        connection,
        "archived_test_dataset_assets",
        ("agent_id", "source_improvement_id", "archived_at"),
    )
    required = {
        "asset_id",
        "agent_id",
        "asset_type",
        "title",
        "body",
        "source_improvement_id",
        "inherited_from",
        "created_at",
        "updated_at",
    }
    if not required.issubset(_table_columns(connection, "governance_assets")):
        return
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO archived_test_dataset_assets (
            legacy_asset_id, agent_id, title, body, source_improvement_id, inherited_from,
            created_at, updated_at, archived_at, reason
        )
        SELECT asset_id, agent_id, title, body, source_improvement_id, inherited_from,
               created_at, updated_at, ?, 'replaced_by_typed_test_dataset'
        FROM governance_assets WHERE asset_type = 'test_dataset'
        """,
        (utc_now(),),
    )
    connection.exec_driver_sql("DELETE FROM governance_assets WHERE asset_type = 'test_dataset'")


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}
