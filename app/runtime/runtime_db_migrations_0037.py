from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import utc_now


def migrate_0037_eval_runs_use_typed_dataset_snapshots(connection: Connection) -> None:
    """Replace legacy global EvalCase bindings with typed TestDataset case bindings."""
    run_columns = _table_columns(connection, "eval_runs")
    item_columns = _table_columns(connection, "eval_run_items")
    if not run_columns and not item_columns:
        return
    if not run_columns or not item_columns:
        present = "eval_runs" if run_columns else "eval_run_items"
        missing = "eval_run_items" if run_columns else "eval_runs"
        raise RuntimeError(f"Partial EvalRun schema: found {present} but missing {missing}")

    # pysqlite defers BEGIN until the first DML statement. Start the write
    # transaction before CREATE TABLE so archive DDL also follows outer rollback.
    connection.exec_driver_sql("UPDATE eval_runs SET eval_run_id = eval_run_id WHERE 0")
    _create_legacy_eval_archives(connection)
    if "dataset_case_id" in item_columns and "eval_case_id" not in item_columns:
        _create_eval_indexes(connection)
        return

    archived_at = utc_now()
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO archived_legacy_eval_runs (
            eval_run_id, dataset_id, created_at, completed_at, status, agent_id,
            agent_version_id, source, payload_json, archived_at, reason
        )
        SELECT eval_run_id, dataset_id, created_at, completed_at, status,
               COALESCE(NULLIF(agent_id, ''), 'main-agent'),
               agent_version_id, source, COALESCE(payload_json, '{}'), ?,
               'replaced_by_typed_test_dataset_snapshot'
        FROM eval_runs
        """,
        (archived_at,),
    )
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO archived_legacy_eval_run_items (
            eval_run_item_id, eval_run_id, eval_case_id, agent_run_id, status,
            score, payload_json, archived_at, reason
        )
        SELECT eval_run_item_id, eval_run_id, eval_case_id, agent_run_id, status,
               score, COALESCE(payload_json, '{}'), ?, 'replaced_by_typed_test_dataset_case'
        FROM eval_run_items
        """,
        (archived_at,),
    )
    connection.exec_driver_sql("DROP TABLE eval_run_items")
    connection.exec_driver_sql("DROP TABLE eval_runs")
    _create_typed_eval_tables(connection)
    _create_eval_indexes(connection)


def _create_legacy_eval_archives(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS archived_legacy_eval_runs (
            eval_run_id VARCHAR(128) NOT NULL PRIMARY KEY,
            dataset_id VARCHAR(128),
            created_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64),
            status VARCHAR(64) NOT NULL,
            agent_id VARCHAR(128) NOT NULL,
            agent_version_id VARCHAR(256),
            source VARCHAR(128) NOT NULL,
            payload_json JSON NOT NULL,
            archived_at VARCHAR(64) NOT NULL,
            reason VARCHAR(512) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS archived_legacy_eval_run_items (
            eval_run_item_id VARCHAR(128) NOT NULL PRIMARY KEY,
            eval_run_id VARCHAR(128) NOT NULL,
            eval_case_id VARCHAR(128) NOT NULL,
            agent_run_id VARCHAR(128),
            status VARCHAR(64) NOT NULL,
            score FLOAT,
            payload_json JSON NOT NULL,
            archived_at VARCHAR(64) NOT NULL,
            reason VARCHAR(512) NOT NULL
        )
        """
    )


def _create_typed_eval_tables(connection: Connection) -> None:
    # Historical migrations must remain executable after their ORM models retire.
    # Migration 0048 archives and removes these temporary typed tables later.
    connection.exec_driver_sql(
        """
        CREATE TABLE eval_runs (
            eval_run_id VARCHAR(128) NOT NULL PRIMARY KEY,
            created_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64),
            status VARCHAR(64) NOT NULL,
            agent_id VARCHAR(128) NOT NULL,
            dataset_id VARCHAR(128) NOT NULL REFERENCES test_datasets(dataset_id),
            agent_version_id VARCHAR(256),
            source VARCHAR(128) NOT NULL,
            payload_json JSON NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE eval_run_items (
            eval_run_item_id VARCHAR(128) NOT NULL PRIMARY KEY,
            eval_run_id VARCHAR(128) NOT NULL REFERENCES eval_runs(eval_run_id) ON DELETE CASCADE,
            dataset_case_id VARCHAR(128) NOT NULL REFERENCES test_dataset_cases(case_id),
            agent_run_id VARCHAR(128),
            status VARCHAR(64) NOT NULL,
            score FLOAT,
            payload_json JSON NOT NULL
        )
        """
    )


def _create_eval_indexes(connection: Connection) -> None:
    for table, columns in {
        "eval_runs": (
            "created_at",
            "completed_at",
            "status",
            "agent_id",
            "dataset_id",
            "agent_version_id",
            "source",
        ),
        "eval_run_items": (
            "eval_run_id",
            "dataset_case_id",
            "agent_run_id",
            "status",
        ),
    }.items():
        for column in columns:
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_eval_run_items_run_dataset_case ON eval_run_items (eval_run_id, dataset_case_id)")


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}
