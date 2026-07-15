from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction


def migrate_0039_test_dataset_revision_provenance(connection: Connection) -> None:
    """Add exact source revision timestamps to typed TestDataset provenance."""
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(test_datasets)")}
    if not columns:
        return
    missing = [
        column
        for column in (
            "source_normalized_feedback_updated_at",
            "source_attribution_updated_at",
            "source_optimization_plan_updated_at",
            "source_execution_updated_at",
        )
        if column not in columns
    ]
    if missing:
        begin_sqlite_write_transaction(connection)
    for column in missing:
        connection.exec_driver_sql(f"ALTER TABLE test_datasets ADD COLUMN {column} VARCHAR(64)")
