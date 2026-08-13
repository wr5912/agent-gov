from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction
from .runtime_db_migrations_0040 import _table_columns


def migrate_0053_agent_test_worker_receipts(connection: Connection) -> None:
    """Add durable worker fencing and isolation receipts without rewriting history."""

    columns = _table_columns(connection, "agent_test_runs")
    if not columns:
        return
    begin_sqlite_write_transaction(connection)
    additions = {
        "source_digest": "VARCHAR(64)",
        "source_tree_sha": "VARCHAR(40)",
        "worker_id": "VARCHAR(128)",
        "claim_generation": "INTEGER NOT NULL DEFAULT 0",
        "container_id": "VARCHAR(128)",
        "receipt_json": "JSON",
    }
    for name, ddl in additions.items():
        if name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE agent_test_runs ADD COLUMN {name} {ddl}")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_runs_worker_id ON agent_test_runs (worker_id)")
