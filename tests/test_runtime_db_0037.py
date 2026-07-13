from __future__ import annotations

import pytest
from app.runtime.runtime_db import make_engine, make_session_factory
from app.runtime.runtime_db_migrations_0037 import migrate_0037_eval_runs_use_typed_dataset_snapshots
from sqlalchemy import create_engine, event


def _legacy_engine(path):
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE test_datasets (dataset_id VARCHAR(128) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE test_dataset_cases (case_id VARCHAR(128) PRIMARY KEY, dataset_id VARCHAR(128))")
        connection.exec_driver_sql("CREATE TABLE eval_cases (eval_case_id VARCHAR(128) PRIMARY KEY)")
        connection.exec_driver_sql(
            """
            CREATE TABLE eval_runs (
                eval_run_id VARCHAR(128) PRIMARY KEY,
                created_at VARCHAR(64) NOT NULL,
                completed_at VARCHAR(64),
                status VARCHAR(64) NOT NULL,
                agent_id VARCHAR(128),
                dataset_id VARCHAR(128),
                agent_version_id VARCHAR(256),
                source VARCHAR(128) NOT NULL,
                payload_json JSON
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE eval_run_items (
                eval_run_item_id VARCHAR(128) PRIMARY KEY,
                eval_run_id VARCHAR(128) NOT NULL REFERENCES eval_runs(eval_run_id),
                eval_case_id VARCHAR(128) NOT NULL REFERENCES eval_cases(eval_case_id),
                agent_run_id VARCHAR(128),
                status VARCHAR(64) NOT NULL,
                score FLOAT,
                payload_json JSON
            )
            """
        )
        connection.exec_driver_sql("INSERT INTO eval_cases VALUES ('evc-legacy')")
        connection.exec_driver_sql("INSERT INTO eval_runs VALUES ('evr-legacy', '2026-07-01', NULL, 'completed', 'main-agent', NULL, NULL, 'legacy', '{}')")
        connection.exec_driver_sql("INSERT INTO eval_run_items VALUES ('evi-legacy', 'evr-legacy', 'evc-legacy', NULL, 'passed', 1.0, '{}')")
    return engine


def _columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def test_0037_archives_legacy_eval_rows_and_rebuilds_typed_foreign_keys(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "upgrade.sqlite3")

    with engine.begin() as connection:
        migrate_0037_eval_runs_use_typed_dataset_snapshots(connection)

    with engine.connect() as connection:
        assert "eval_case_id" not in _columns(connection, "eval_run_items")
        assert "dataset_case_id" in _columns(connection, "eval_run_items")
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM eval_runs").scalar_one() == 0
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM archived_legacy_eval_runs").scalar_one() == 1
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM archived_legacy_eval_run_items").scalar_one() == 1
        item_foreign_keys = {(str(row[2]), str(row[3]), str(row[4])) for row in connection.exec_driver_sql("PRAGMA foreign_key_list(eval_run_items)")}
        assert ("test_dataset_cases", "dataset_case_id", "case_id") in item_foreign_keys
        run_foreign_keys = {(str(row[2]), str(row[3]), str(row[4])) for row in connection.exec_driver_sql("PRAGMA foreign_key_list(eval_runs)")}
        assert ("test_datasets", "dataset_id", "dataset_id") in run_foreign_keys
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_0037_ddl_and_archival_roll_back_as_one_sqlite_transaction(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "rollback.sqlite3")

    with pytest.raises(RuntimeError, match="force rollback"):
        with engine.begin() as connection:
            migrate_0037_eval_runs_use_typed_dataset_snapshots(connection)
            raise RuntimeError("force rollback")

    with engine.connect() as connection:
        assert "eval_case_id" in _columns(connection, "eval_run_items")
        assert "dataset_case_id" not in _columns(connection, "eval_run_items")
        assert not _columns(connection, "archived_legacy_eval_runs")
        assert not _columns(connection, "archived_legacy_eval_run_items")
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM eval_runs").scalar_one() == 1
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM eval_run_items").scalar_one() == 1


def test_fresh_schema_uses_typed_eval_foreign_keys(tmp_path) -> None:
    path = tmp_path / "fresh.sqlite3"
    make_session_factory(path)
    engine = make_engine(path)

    with engine.connect() as connection:
        assert "dataset_case_id" in _columns(connection, "eval_run_items")
        assert "eval_case_id" not in _columns(connection, "eval_run_items")
        not_null = {str(row[1]): bool(row[3]) for row in connection.exec_driver_sql("PRAGMA table_info(eval_runs)")}
        assert not_null["dataset_id"] is True
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_0037_rejects_partial_eval_schema(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE eval_runs (eval_run_id VARCHAR(128) PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="found eval_runs but missing eval_run_items"):
        with engine.begin() as connection:
            migrate_0037_eval_runs_use_typed_dataset_snapshots(connection)
