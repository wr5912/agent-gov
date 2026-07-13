from __future__ import annotations

import pytest
from app.runtime.runtime_db import make_engine, make_session_factory
from app.runtime.runtime_db_migrations_0041 import migrate_0041_agent_registry_provisioning_saga
from sqlalchemy import create_engine

EXPECTED_COLUMNS = {
    "provision_state",
    "provision_token",
    "provision_started_at",
    "provision_previous_json",
}


def _columns(connection) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}


def _create_legacy_table(connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE agent_registry (
            agent_id VARCHAR(128) PRIMARY KEY,
            name VARCHAR(256) NOT NULL,
            category VARCHAR(32) NOT NULL,
            workspace_dir VARCHAR(2048) NOT NULL,
            created_at VARCHAR(64) NOT NULL
        )
        """
    )


def test_0041_adds_provisioning_columns_and_backfills_ready(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}", future=True)
    with engine.begin() as connection:
        _create_legacy_table(connection)
        connection.exec_driver_sql("INSERT INTO agent_registry VALUES ('a', 'A', 'business', '/workspace', 'now')")
        migrate_0041_agent_registry_provisioning_saga(connection)
        migrate_0041_agent_registry_provisioning_saga(connection)

    with engine.connect() as connection:
        assert _columns(connection) >= EXPECTED_COLUMNS
        assert connection.exec_driver_sql("SELECT provision_state FROM agent_registry WHERE agent_id = 'a'").scalar_one() == "ready"
        indexes = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA index_list(agent_registry)")}
        assert "ix_agent_registry_provision_state" in indexes


def test_fresh_agent_registry_schema_contains_provisioning_columns(tmp_path) -> None:
    path = tmp_path / "fresh.sqlite3"
    make_session_factory(path)

    with make_engine(path).connect() as connection:
        assert _columns(connection) >= EXPECTED_COLUMNS


def test_0041_schema_changes_roll_back_with_outer_transaction(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.sqlite3'}", future=True)
    with engine.begin() as connection:
        _create_legacy_table(connection)

    with pytest.raises(RuntimeError, match="force rollback"):
        with engine.begin() as connection:
            migrate_0041_agent_registry_provisioning_saga(connection)
            raise RuntimeError("force rollback")

    with engine.connect() as connection:
        assert _columns(connection).isdisjoint(EXPECTED_COLUMNS)
