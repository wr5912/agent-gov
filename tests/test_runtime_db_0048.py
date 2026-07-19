from __future__ import annotations

import json

import pytest
from app.runtime.runtime_db_migrations_0048 import (
    SUPERSEDED_TEST_TABLES,
    migrate_0048_workspace_pytest_source_of_truth,
)
from sqlalchemy import create_engine


def _legacy_engine(path):
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        for table_name in SUPERSEDED_TEST_TABLES:
            connection.exec_driver_sql(
                f'CREATE TABLE "{table_name}" (record_id TEXT PRIMARY KEY, payload_json JSON)'
            )
            connection.exec_driver_sql(
                f'INSERT INTO "{table_name}" VALUES (?, ?)',
                (f"{table_name}-1", json.dumps({"source": table_name}, ensure_ascii=False)),
            )
        connection.exec_driver_sql(
            "CREATE TABLE agent_change_sets (change_set_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        for status in (
            "regression_running",
            "regression_review_required",
            "regression_passed",
            "regression_failed",
            "approved",
        ):
            connection.exec_driver_sql(
                "INSERT INTO agent_change_sets VALUES (?, ?)",
                (f"agc-{status}", status),
            )
    return engine


def _tables(connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def test_0048_archives_old_test_rows_drops_tables_and_normalizes_change_sets(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "upgrade.sqlite3")

    with engine.begin() as connection:
        migrate_0048_workspace_pytest_source_of_truth(connection)

    with engine.connect() as connection:
        assert set(SUPERSEDED_TEST_TABLES).isdisjoint(_tables(connection))
        archived = connection.exec_driver_sql(
            "SELECT source_table, row_json, reason FROM archived_legacy_evaluation_rows ORDER BY source_table"
        ).fetchall()
        states = dict(
            connection.exec_driver_sql(
                "SELECT change_set_id, status FROM agent_change_sets ORDER BY change_set_id"
            ).fetchall()
        )

    assert len(archived) == len(SUPERSEDED_TEST_TABLES)
    assert {row[0] for row in archived} == set(SUPERSEDED_TEST_TABLES)
    assert all(row[2] == "replaced_by_versioned_workspace_pytest_suite" for row in archived)
    assert all(json.loads(row[1])["record_id"].endswith("-1") for row in archived)
    assert states["agc-approved"] == "approved"
    assert {
        state for change_set_id, state in states.items() if change_set_id != "agc-approved"
    } == {"candidate_committed"}


def test_0048_is_idempotent_after_old_tables_are_removed(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "idempotent.sqlite3")
    with engine.begin() as connection:
        migrate_0048_workspace_pytest_source_of_truth(connection)
    with engine.begin() as connection:
        migrate_0048_workspace_pytest_source_of_truth(connection)

    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM archived_legacy_evaluation_rows"
        ).scalar_one() == len(SUPERSEDED_TEST_TABLES)


def test_0048_archive_drop_and_status_update_roll_back_together(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "rollback.sqlite3")

    with pytest.raises(RuntimeError, match="force rollback"):
        with engine.begin() as connection:
            migrate_0048_workspace_pytest_source_of_truth(connection)
            raise RuntimeError("force rollback")

    with engine.connect() as connection:
        assert set(SUPERSEDED_TEST_TABLES) <= _tables(connection)
        assert "archived_legacy_evaluation_rows" not in _tables(connection)
        assert connection.exec_driver_sql(
            "SELECT status FROM agent_change_sets WHERE change_set_id = 'agc-regression_failed'"
        ).scalar_one() == "regression_failed"
