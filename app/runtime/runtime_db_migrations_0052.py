from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction
from .runtime_db_migrations_0040 import _table_columns


def migrate_0052_agent_test_asset_schedules(connection: Connection) -> None:
    """Add per-Agent test schedules, durable occurrences and run provenance."""

    begin_sqlite_write_transaction(connection)
    run_columns = _table_columns(connection, "agent_test_runs")
    if run_columns and "schedule_id" not in run_columns:
        connection.exec_driver_sql("ALTER TABLE agent_test_runs ADD COLUMN schedule_id VARCHAR(128)")
    if run_columns and "scheduled_for" not in run_columns:
        connection.exec_driver_sql("ALTER TABLE agent_test_runs ADD COLUMN scheduled_for VARCHAR(64)")

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_test_schedules (
            schedule_id VARCHAR(128) NOT NULL PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT 0,
            cron_expression VARCHAR(128) NOT NULL,
            timezone VARCHAR(128) NOT NULL,
            next_run_at VARCHAR(64),
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_test_schedule_events (
            schedule_event_id VARCHAR(128) NOT NULL PRIMARY KEY,
            schedule_id VARCHAR(128) NOT NULL,
            agent_id VARCHAR(128) NOT NULL,
            scheduled_for VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            resolved_commit_sha VARCHAR(64),
            test_run_id VARCHAR(128),
            detail_json JSON NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64),
            CONSTRAINT ux_agent_test_schedule_events_occurrence UNIQUE (schedule_id, scheduled_for),
            FOREIGN KEY(schedule_id) REFERENCES agent_test_schedules (schedule_id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_test_schedules_agent_id ON agent_test_schedules (agent_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_schedules_enabled ON agent_test_schedules (enabled)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_schedules_next_run_at ON agent_test_schedules (next_run_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_schedule_events_agent_created ON agent_test_schedule_events (agent_id, created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_schedule_events_status ON agent_test_schedule_events (status)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_schedule_events_test_run_id ON agent_test_schedule_events (test_run_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_runs_schedule_id ON agent_test_runs (schedule_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_runs_scheduled_for ON agent_test_runs (scheduled_for)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_test_runs_schedule_occurrence ON agent_test_runs (schedule_id, scheduled_for)")
