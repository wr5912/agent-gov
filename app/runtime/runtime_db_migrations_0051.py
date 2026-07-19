from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import utc_now
from .runtime_db_migrations_0040 import _archive_table_rows, _create_archive_table, _table_columns


def migrate_0051_replace_regression_design_with_pytest_code(connection: Connection) -> None:
    """Archive the natural-language design rows and install the pytest-code contract."""

    columns = _table_columns(connection, "regression_test_designs")
    if "tests_json" in columns and "cases_json" not in columns:
        return
    _create_archive_table(connection)
    if columns:
        _archive_table_rows(
            connection,
            table_name="regression_test_designs",
            archived_at=utc_now(),
            reason="Replaced natural-language expected behavior and checkpoints with executable pytest code.",
        )
        connection.exec_driver_sql("DROP TABLE regression_test_designs")
    connection.exec_driver_sql(
        """
        CREATE TABLE regression_test_designs (
            regression_test_design_id VARCHAR(128) NOT NULL PRIMARY KEY,
            improvement_id VARCHAR(128) NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            tests_json JSON NOT NULL,
            no_action_reason TEXT NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL DEFAULT 'draft',
            generated_by VARCHAR(32) NOT NULL DEFAULT 'governor',
            generation_trace_id VARCHAR(256) NOT NULL DEFAULT '',
            generation_trace_url VARCHAR(2048) NOT NULL DEFAULT '',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX ix_regression_test_designs_improvement_id "
        "ON regression_test_designs (improvement_id)"
    )
