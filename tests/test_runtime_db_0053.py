from __future__ import annotations

import json

from app.runtime.runtime_db_migrations_0053 import migrate_0053_agent_test_worker_receipts
from sqlalchemy import create_engine


def test_0053_adds_worker_fencing_and_preserves_historical_passed_report() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    historical_report = {"exit_code": 0, "items": [{"nodeid": "tests/test_old.py::test_old"}]}
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_test_runs (
                test_run_id VARCHAR(128) PRIMARY KEY,
                agent_id VARCHAR(128) NOT NULL,
                commit_sha VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL,
                report_json JSON
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_test_runs VALUES (?, ?, ?, 'passed', ?)",
            ("atr-history", "agent-a", "a" * 40, json.dumps(historical_report)),
        )

        migrate_0053_agent_test_worker_receipts(connection)
        migrate_0053_agent_test_worker_receipts(connection)

        columns = {str(row[1]): row for row in connection.exec_driver_sql("PRAGMA table_info(agent_test_runs)")}
        assert {"source_digest", "source_tree_sha", "worker_id", "claim_generation", "container_id", "receipt_json"} <= set(columns)
        assert int(columns["claim_generation"][3]) == 1
        row = connection.exec_driver_sql(
            "SELECT status, report_json, receipt_json, claim_generation FROM agent_test_runs WHERE test_run_id = 'atr-history'"
        ).one()
        assert row.status == "passed"
        assert json.loads(row.report_json) == historical_report
        assert row.receipt_json is None
        assert row.claim_generation == 0

        indexes = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA index_list(agent_test_runs)")}
        assert "ix_agent_test_runs_worker_id" in indexes
