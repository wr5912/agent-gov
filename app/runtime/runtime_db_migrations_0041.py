from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction


def migrate_0041_agent_registry_provisioning_saga(connection: Connection) -> None:
    """Add hidden reservation state for DB + workspace Agent creation."""
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}
    if not columns:
        return
    missing = [
        column
        for column in (
            "provision_state",
            "provision_token",
            "provision_started_at",
            "provision_previous_json",
        )
        if column not in columns
    ]
    begin_sqlite_write_transaction(connection)
    if "provision_state" in missing:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN provision_state VARCHAR(32) NOT NULL DEFAULT 'ready'")
    if "provision_token" in missing:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN provision_token VARCHAR(64)")
    if "provision_started_at" in missing:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN provision_started_at VARCHAR(64)")
    if "provision_previous_json" in missing:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN provision_previous_json JSON")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_registry_provision_state ON agent_registry (provision_state)")
