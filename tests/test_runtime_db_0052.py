from __future__ import annotations

import pytest
from app.runtime.runtime_db_migrations_0052 import migrate_0052_agent_test_asset_schedules
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError


def test_0052_adds_schedule_tables_run_provenance_and_occurrence_idempotency() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_test_runs (
                test_run_id VARCHAR(128) PRIMARY KEY,
                agent_id VARCHAR(128) NOT NULL,
                commit_sha VARCHAR(64) NOT NULL
            )
            """
        )
        migrate_0052_agent_test_asset_schedules(connection)
        migrate_0052_agent_test_asset_schedules(connection)
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_test_runs)")}
        assert {"schedule_id", "scheduled_for"} <= columns
        tables = {str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"agent_test_schedules", "agent_test_schedule_events"} <= tables

        connection.exec_driver_sql(
            "INSERT INTO agent_test_schedules VALUES "
            "('atsc-a', 'agent-a', 1, '0 2 * * *', 'UTC', '2026-07-21T02:00:00+00:00', "
            "'2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_test_schedule_events "
            "(schedule_event_id, schedule_id, agent_id, scheduled_for, status, detail_json, created_at) "
            "VALUES ('atse-a', 'atsc-a', 'agent-a', '2026-07-21T02:00:00+00:00', 'pending', '{}', '2026-07-20T00:00:00+00:00')"
        )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO agent_test_schedule_events "
                "(schedule_event_id, schedule_id, agent_id, scheduled_for, status, detail_json, created_at) "
                "VALUES ('atse-b', 'atsc-a', 'agent-a', '2026-07-21T02:00:00+00:00', 'pending', '{}', '2026-07-20T00:00:00+00:00')"
            )
