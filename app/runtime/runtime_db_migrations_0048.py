from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction, utc_now
from .runtime_db_migrations_0040 import _archive_table_rows, _create_archive_table, _table_columns

SUPERSEDED_TEST_TABLES = (
    "eval_run_items",
    "eval_runs",
    "test_dataset_revisions",
    "test_dataset_cases",
    "test_datasets",
    "archived_test_dataset_assets",
)


def migrate_0048_workspace_pytest_source_of_truth(connection: Connection) -> None:
    """Archive and remove DB-authoritative test datasets and evaluation runs."""

    begin_sqlite_write_transaction(connection)
    _create_archive_table(connection)
    archived_at = utc_now()
    if "status" in _table_columns(connection, "agent_change_sets"):
        connection.exec_driver_sql(
            """
            UPDATE agent_change_sets
            SET status = 'candidate_committed'
            WHERE status IN (
                'regression_running',
                'regression_review_required',
                'regression_passed',
                'regression_failed'
            )
            """
        )
    for table_name in SUPERSEDED_TEST_TABLES:
        if _table_columns(connection, table_name):
            _archive_table_rows(
                connection,
                table_name=table_name,
                archived_at=archived_at,
                reason="replaced_by_versioned_workspace_pytest_suite",
            )
    for table_name in SUPERSEDED_TEST_TABLES:
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')
