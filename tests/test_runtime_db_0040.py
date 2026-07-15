from __future__ import annotations

import json

import pytest
from app.runtime.runtime_db import make_engine, make_session_factory
from app.runtime.runtime_db_migrations_0040 import (
    LEGACY_EVALUATION_TABLES,
    LEGACY_REGRESSION_ASSET_TYPE,
    migrate_0040_archive_and_remove_legacy_evaluation_chain,
)
from sqlalchemy import create_engine


def _legacy_engine(path):
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE eval_cases (eval_case_id TEXT PRIMARY KEY, payload_json JSON, note TEXT)")
        connection.exec_driver_sql("CREATE TABLE eval_case_revisions (revision_id TEXT PRIMARY KEY, eval_case_id TEXT, snapshot_json JSON)")
        connection.exec_driver_sql("CREATE TABLE eval_case_governance_events (event_id TEXT PRIMARY KEY, eval_case_id TEXT, before_json JSON)")
        connection.exec_driver_sql("CREATE TABLE scenario_packs (scenario_pack_id TEXT PRIMARY KEY, payload_json JSON)")
        connection.exec_driver_sql("CREATE TABLE archived_legacy_eval_runs (eval_run_id TEXT PRIMARY KEY, payload_json JSON)")
        connection.exec_driver_sql("CREATE TABLE archived_legacy_eval_run_items (eval_run_item_id TEXT PRIMARY KEY, payload_json JSON)")
        connection.exec_driver_sql("CREATE TABLE agent_jobs (job_id TEXT PRIMARY KEY, job_type TEXT, input_json JSON)")
        connection.exec_driver_sql(
            """
            CREATE TABLE governance_assets (
                asset_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source_improvement_id TEXT NOT NULL,
                inherited_from TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO eval_cases VALUES (?, ?, ?)",
            ("evc-1", '{"nested":{"answer":42}}', "保留原始文本"),
        )
        connection.exec_driver_sql(
            "INSERT INTO eval_case_revisions VALUES (?, ?, ?)",
            ("rev-1", "evc-1", '{"revision":1}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO eval_case_governance_events VALUES (?, ?, ?)",
            ("evt-1", "evc-1", '{"status":"draft"}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO scenario_packs VALUES (?, ?)",
            ("pack-1", '{"eval_case_ids":["evc-1"]}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO archived_legacy_eval_runs VALUES (?, ?)",
            ("evr-old", '{"status":"completed"}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO archived_legacy_eval_run_items VALUES (?, ?)",
            ("evi-old", '{"score":1.0}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_jobs VALUES (?, ?, ?)",
            ("job-eval", "eval_case_generation", '{"source":"legacy"}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_jobs VALUES (?, ?, ?)",
            ("job-keep", "attribution", '{"source":"active"}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO governance_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ast-regression",
                "agent-a",
                LEGACY_REGRESSION_ASSET_TYPE,
                "旧独立回归资产",
                '{"case":"verbatim"}',
                "imp-a",
                "",
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO governance_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ast-methodology",
                "agent-a",
                "methodology",
                "保留的方法论",
                "步骤",
                "imp-a",
                "",
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
            ),
        )
    return engine


def _tables(connection) -> set[str]:
    return {str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def test_0040_archives_complete_raw_rows_then_removes_legacy_chain(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "upgrade.sqlite3")

    with engine.begin() as connection:
        migrate_0040_archive_and_remove_legacy_evaluation_chain(connection)

    with engine.connect() as connection:
        tables = _tables(connection)
        assert set(LEGACY_EVALUATION_TABLES).isdisjoint(tables)
        assert "archived_legacy_evaluation_rows" in tables
        assert connection.exec_driver_sql("SELECT job_id FROM agent_jobs ORDER BY job_id").scalars().all() == ["job-keep"]
        archived = connection.exec_driver_sql("SELECT source_table, row_json FROM archived_legacy_evaluation_rows").fetchall()
        retained_assets = connection.exec_driver_sql("SELECT asset_id, asset_type FROM governance_assets ORDER BY asset_id").fetchall()

    assert len(archived) == 8
    assert retained_assets == [("ast-methodology", "methodology")]
    rows_by_table = {str(source_table): json.loads(str(row_json)) for source_table, row_json in archived}
    assert rows_by_table["eval_cases"] == {
        "eval_case_id": "evc-1",
        "note": "保留原始文本",
        "payload_json": '{"nested":{"answer":42}}',
    }
    assert rows_by_table["agent_jobs"] == {
        "input_json": '{"source":"legacy"}',
        "job_id": "job-eval",
        "job_type": "eval_case_generation",
    }
    assert rows_by_table["governance_assets"] == {
        "agent_id": "agent-a",
        "asset_id": "ast-regression",
        "asset_type": LEGACY_REGRESSION_ASSET_TYPE,
        "body": '{"case":"verbatim"}',
        "created_at": "2026-07-01T00:00:00+00:00",
        "inherited_from": "",
        "source_improvement_id": "imp-a",
        "title": "旧独立回归资产",
        "updated_at": "2026-07-02T00:00:00+00:00",
    }


def test_0040_is_idempotent_after_source_tables_are_removed(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "idempotent.sqlite3")
    with engine.begin() as connection:
        migrate_0040_archive_and_remove_legacy_evaluation_chain(connection)
    with engine.begin() as connection:
        migrate_0040_archive_and_remove_legacy_evaluation_chain(connection)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM archived_legacy_evaluation_rows").scalar_one() == 8
        assert connection.exec_driver_sql("SELECT asset_id FROM governance_assets ORDER BY asset_id").scalars().all() == ["ast-methodology"]


def test_0040_archive_and_destructive_changes_roll_back_together(tmp_path) -> None:
    engine = _legacy_engine(tmp_path / "rollback.sqlite3")

    with pytest.raises(RuntimeError, match="force rollback"):
        with engine.begin() as connection:
            migrate_0040_archive_and_remove_legacy_evaluation_chain(connection)
            raise RuntimeError("force rollback")

    with engine.connect() as connection:
        tables = _tables(connection)
        assert set(LEGACY_EVALUATION_TABLES) <= tables
        assert "archived_legacy_evaluation_rows" not in tables
        assert connection.exec_driver_sql("SELECT job_id FROM agent_jobs ORDER BY job_id").scalars().all() == ["job-eval", "job-keep"]
        assert connection.exec_driver_sql("SELECT asset_id FROM governance_assets ORDER BY asset_id").scalars().all() == ["ast-methodology", "ast-regression"]


def test_fresh_schema_has_only_typed_evaluation_tables(tmp_path) -> None:
    path = tmp_path / "fresh.sqlite3"
    make_session_factory(path)
    engine = make_engine(path)

    with engine.connect() as connection:
        tables = _tables(connection)
        assert set(LEGACY_EVALUATION_TABLES).isdisjoint(tables)
        assert {"test_datasets", "test_dataset_cases", "eval_runs", "eval_run_items"} <= tables
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM governance_assets WHERE asset_type = ?",
                (LEGACY_REGRESSION_ASSET_TYPE,),
            ).scalar_one()
            == 0
        )
        assert (
            connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0040_archive_and_remove_legacy_evaluation_chain'").one()[0]
            == "0040_archive_and_remove_legacy_evaluation_chain"
        )
