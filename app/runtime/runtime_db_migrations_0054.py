from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction
from .runtime_db_migrations_0040 import _table_columns


def migrate_0054_workspace_import_diagnostics(connection: Connection) -> None:
    """Promote complete import-suite diagnostics to first-class audit columns."""

    columns = _table_columns(connection, "agent_workspace_import_records")
    if not columns:
        return
    begin_sqlite_write_transaction(connection)
    if "suite_status" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_workspace_import_records ADD COLUMN suite_status VARCHAR(32)")
    if "diagnostics_json" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_workspace_import_records ADD COLUMN diagnostics_json JSON NOT NULL DEFAULT '[]'")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_workspace_import_records_suite_status ON agent_workspace_import_records (suite_status)")
    connection.exec_driver_sql(
        """
        UPDATE agent_workspace_import_records
        SET diagnostics_json = CASE
            WHEN json_valid(suite_json)
                THEN COALESCE(json_extract(suite_json, '$.diagnostics'), json('[]'))
            ELSE json('[]')
        END
        WHERE status = 'accepted'
          AND COALESCE(json_array_length(diagnostics_json), 0) = 0
        """
    )
    connection.exec_driver_sql(
        """
        UPDATE agent_workspace_import_records
        SET suite_status = CASE
            WHEN EXISTS (
                SELECT 1 FROM json_each(diagnostics_json)
                WHERE json_extract(json_each.value, '$.level') = 'error'
            ) THEN 'invalid'
            WHEN EXISTS (
                SELECT 1 FROM json_each(diagnostics_json)
                WHERE json_extract(json_each.value, '$.level') = 'warning'
            ) THEN 'warning'
            ELSE 'ready'
        END
        WHERE status = 'accepted'
          AND suite_status IS NULL
        """
    )
