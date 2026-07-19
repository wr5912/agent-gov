from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from app.runtime.runtime_db_migrations_0050 import migrate_0050_deduplicate_active_agent_test_runs


def test_0050_interrupts_duplicate_active_runs_and_adds_exact_target_index(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_test_runs (
                test_run_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                change_set_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error_json JSON
            )
            """
        )
        for run_id, created_at in (("atr-first", "2026-07-18T00:00:00Z"), ("atr-second", "2026-07-18T00:00:01Z")):
            connection.exec_driver_sql(
                "INSERT INTO agent_test_runs VALUES (?, 'agent-a', ?, NULL, 'queued', ?, NULL, '{}')",
                (run_id, "a" * 40, created_at),
            )

        migrate_0050_deduplicate_active_agent_test_runs(connection)

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT test_run_id, status, error_json FROM agent_test_runs ORDER BY created_at"
        ).fetchall()
        assert rows[0][0:2] == ("atr-first", "queued")
        assert rows[1][0:2] == ("atr-second", "interrupted")
        assert json.loads(rows[1][2])["error_code"] == "AGENT_TEST_RUN_DUPLICATE_RECONCILED"

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO agent_test_runs VALUES ('atr-third', 'agent-a', ?, NULL, 'running', '', NULL, '{}')",
                ("a" * 40,),
            )


def test_0050_allows_new_run_after_prior_target_is_terminal(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'terminal.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_test_runs (
                test_run_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                change_set_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error_json JSON
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_test_runs VALUES ('atr-old', 'agent-a', ?, 'agc-a', 'failed', '', '', '{}')",
            ("a" * 40,),
        )
        migrate_0050_deduplicate_active_agent_test_runs(connection)
        connection.exec_driver_sql(
            "INSERT INTO agent_test_runs VALUES ('atr-new', 'agent-a', ?, 'agc-a', 'queued', '', NULL, '{}')",
            ("a" * 40,),
        )
