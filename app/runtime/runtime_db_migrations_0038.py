from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction


def migrate_0038_remove_agent_registry_requires_web_hitl(connection: Connection) -> None:
    """Remove the cached HITL projection; project settings are the only policy source."""
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}
    if "requires_web_hitl" in columns:
        begin_sqlite_write_transaction(connection)
        connection.exec_driver_sql("ALTER TABLE agent_registry DROP COLUMN requires_web_hitl")
