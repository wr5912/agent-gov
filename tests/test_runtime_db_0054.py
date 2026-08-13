from __future__ import annotations

import json

from app.runtime.runtime_db_migrations_0054 import migrate_0054_workspace_import_diagnostics
from sqlalchemy import create_engine


def test_0054_backfills_complete_import_diagnostics_and_suite_status_idempotently() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    diagnostics = [
        {"level": "warning", "code": "WARN", "message": "warning"},
        {"level": "error", "code": "ERROR", "message": "error"},
    ]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_workspace_import_records (
                import_id VARCHAR(128) PRIMARY KEY,
                status VARCHAR(32) NOT NULL,
                suite_json JSON NOT NULL,
                warnings_json JSON NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_workspace_import_records VALUES (?, 'accepted', ?, ?)",
            (
                "awi-accepted",
                json.dumps({"diagnostics": diagnostics}),
                json.dumps([diagnostics[0]]),
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_workspace_import_records VALUES (?, 'failed', '{}', '[]')",
            ("awi-failed",),
        )

        migrate_0054_workspace_import_diagnostics(connection)
        migrate_0054_workspace_import_diagnostics(connection)

        accepted = connection.exec_driver_sql(
            "SELECT suite_status, diagnostics_json FROM agent_workspace_import_records WHERE import_id = 'awi-accepted'"
        ).one()
        failed = connection.exec_driver_sql("SELECT suite_status, diagnostics_json FROM agent_workspace_import_records WHERE import_id = 'awi-failed'").one()
        assert accepted.suite_status == "invalid"
        assert json.loads(accepted.diagnostics_json) == diagnostics
        assert failed.suite_status is None
        assert json.loads(failed.diagnostics_json) == []

        indexes = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA index_list(agent_workspace_import_records)")}
        assert "ix_agent_workspace_import_records_suite_status" in indexes
