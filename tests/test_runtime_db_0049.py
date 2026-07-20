from __future__ import annotations

import json

import pytest
from app.runtime.runtime_db import make_session_factory
from app.runtime.runtime_db_migrations_0049 import migrate_0049_rename_regression_test_design
from sqlalchemy import create_engine


def _create_legacy_assessment(connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE regression_assessments (
            regression_assessment_id TEXT PRIMARY KEY,
            improvement_id TEXT NOT NULL UNIQUE,
            summary TEXT,
            cases_json JSON,
            suggested_gate_thresholds_json JSON,
            status TEXT,
            generated_by TEXT,
            generation_trace_id TEXT,
            generation_trace_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        "INSERT INTO regression_assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "reg-old",
            "imp-1",
            "测试设计",
            json.dumps([{"prompt": "重试"}]),
            json.dumps({"通过率": "100%"}, ensure_ascii=False),
            "confirmed",
            "governor",
            "trace-1",
            "",
            "2026-07-18T00:00:00Z",
            "2026-07-18T00:01:00Z",
        ),
    )


def _legacy_engine(path):
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        _create_legacy_assessment(connection)
        connection.exec_driver_sql("CREATE TABLE agent_jobs (job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO agent_jobs VALUES ('job-1', 'regression_assessment')")
        connection.exec_driver_sql("CREATE TABLE agent_change_sets (change_set_id TEXT PRIMARY KEY, payload_json JSON)")
        connection.exec_driver_sql(
            "INSERT INTO agent_change_sets VALUES (?, ?)",
            (
                "agc-1",
                json.dumps(
                    {
                        "title": "保留",
                        "latest_eval_run_id": None,
                        "latest_eval_run": {"status": "completed"},
                        "regression_attempt_id": "old",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    return engine


def _tables(connection) -> set[str]:
    return {str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def test_0049_moves_test_design_rows_and_removes_old_payload_fields(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "upgrade.sqlite3")

    with engine.begin() as connection:
        migrate_0049_rename_regression_test_design(connection)

    with engine.connect() as connection:
        assert "regression_assessments" not in _tables(connection)
        row = connection.exec_driver_sql("SELECT regression_test_design_id, improvement_id, status FROM regression_test_designs").one()
        assert tuple(row) == ("reg-old", "imp-1", "confirmed")
        assert connection.exec_driver_sql("SELECT job_type FROM agent_jobs WHERE job_id = 'job-1'").scalar_one() == "regression_test_design"
        payload = json.loads(connection.exec_driver_sql("SELECT payload_json FROM agent_change_sets WHERE change_set_id = 'agc-1'").scalar_one())
        assert payload == {"title": "保留"}


def test_0049_rejects_ambiguous_dual_table_data_and_rolls_back(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "ambiguous.sqlite3")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE regression_test_designs (
                regression_test_design_id TEXT PRIMARY KEY,
                improvement_id TEXT NOT NULL UNIQUE,
                summary TEXT,
                cases_json JSON,
                suggested_gate_thresholds_json JSON,
                status TEXT,
                generated_by TEXT,
                generation_trace_id TEXT,
                generation_trace_url TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        connection.exec_driver_sql("INSERT INTO regression_test_designs VALUES ('new', 'imp-2', '', '[]', '{}', 'draft', 'governor', '', '', '', '')")

    with pytest.raises(RuntimeError, match="ambiguous merge"):
        with engine.begin() as connection:
            migrate_0049_rename_regression_test_design(connection)

    with engine.connect() as connection:
        assert "regression_assessments" in _tables(connection)
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM regression_test_designs").scalar_one() == 1


def test_fresh_runtime_schema_exposes_only_regression_test_design(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "fresh.sqlite3")
    with factory() as db:
        tables = _tables(db.connection())
        assert "regression_test_designs" in tables
        assert "regression_assessments" not in tables


def test_schema_init_archives_legacy_rows_when_create_all_precreates_final_table(tmp_path) -> None:
    db_path = tmp_path / "precreated-final.sqlite3"
    make_session_factory(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as connection:
        _create_legacy_assessment(connection)
        connection.exec_driver_sql(
            "DELETE FROM schema_migrations WHERE version IN (?, ?, ?)",
            (
                "0049_rename_regression_test_design",
                "0050_deduplicate_active_agent_test_runs",
                "0051_replace_regression_design_with_pytest_code",
            ),
        )
    engine.dispose()

    factory = make_session_factory(db_path)

    with factory() as db:
        connection = db.connection()
        tables = _tables(connection)
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(regression_test_designs)")}
        archived = connection.exec_driver_sql(
            "SELECT row_json, reason FROM archived_legacy_evaluation_rows WHERE source_table = 'regression_assessments'"
        ).one()
        applied = {
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT version FROM schema_migrations WHERE version IN (?, ?)",
                ("0049_rename_regression_test_design", "0051_replace_regression_design_with_pytest_code"),
            ).fetchall()
        }

        assert "regression_assessments" not in tables
        assert {"tests_json", "no_action_reason"} <= columns
        assert "cases_json" not in columns
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM regression_test_designs").scalar_one() == 0
        assert "reg-old" in archived[0]
        assert archived[1] == "replaced_by_executable_pytest_contract"
        assert applied == {
            "0049_rename_regression_test_design",
            "0051_replace_regression_design_with_pytest_code",
        }
