from __future__ import annotations

import json

from app.runtime.runtime_db_migrations_0051 import migrate_0051_replace_regression_design_with_pytest_code
from sqlalchemy import create_engine


def test_migration_0051_archives_old_designs_and_installs_code_contract() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE regression_test_designs (
                regression_test_design_id VARCHAR(128) PRIMARY KEY,
                improvement_id VARCHAR(128),
                summary TEXT,
                cases_json JSON,
                suggested_gate_thresholds_json JSON,
                status VARCHAR(32),
                generated_by VARCHAR(32),
                generation_trace_id VARCHAR(256),
                generation_trace_url VARCHAR(2048),
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO regression_test_designs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "reg-old",
                "imp-old",
                "old",
                json.dumps([{"prompt": "x", "expected_behavior": "y", "checkpoints": ["z"]}]),
                json.dumps({"pass_rate": "95%"}),
                "confirmed",
                "heuristic",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
            ),
        )

        migrate_0051_replace_regression_design_with_pytest_code(connection)

        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(regression_test_designs)")}
        assert "tests_json" in columns
        assert "no_action_reason" in columns
        assert "cases_json" not in columns
        assert "suggested_gate_thresholds_json" not in columns
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM regression_test_designs").scalar_one() == 0
        archived = connection.exec_driver_sql(
            "SELECT source_key, row_json, reason FROM archived_legacy_evaluation_rows "
            "WHERE source_table = 'regression_test_designs'"
        ).one()
        assert "reg-old" in archived[0]
        assert "expected_behavior" in archived[1]
        assert "executable pytest code" in archived[2]

        migrate_0051_replace_regression_design_with_pytest_code(connection)
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM regression_test_designs").scalar_one() == 0
