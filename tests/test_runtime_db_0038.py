from __future__ import annotations

import pytest
from app.runtime.runtime_db import make_engine, make_session_factory
from app.runtime.runtime_db_migrations_0038 import (
    migrate_0038_remove_agent_registry_requires_web_hitl,
)
from sqlalchemy import create_engine


def _columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def test_0038_removes_cached_hitl_projection_from_legacy_registry(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_registry (
                agent_id VARCHAR(128) PRIMARY KEY,
                name VARCHAR(256) NOT NULL,
                requires_web_hitl BOOLEAN NOT NULL DEFAULT 0
            )
            """
        )
        connection.exec_driver_sql("INSERT INTO agent_registry VALUES ('main-agent', 'main-agent', 1)")
        migrate_0038_remove_agent_registry_requires_web_hitl(connection)

    with engine.connect() as connection:
        assert "requires_web_hitl" not in _columns(connection, "agent_registry")
        assert connection.exec_driver_sql("SELECT agent_id, name FROM agent_registry").one() == ("main-agent", "main-agent")


def test_fresh_registry_schema_has_no_cached_hitl_projection(tmp_path) -> None:
    path = tmp_path / "fresh.sqlite3"
    make_session_factory(path)

    with make_engine(path).connect() as connection:
        assert "requires_web_hitl" not in _columns(connection, "agent_registry")


def test_0038_drop_column_rolls_back_with_outer_transaction(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE agent_registry (agent_id VARCHAR(128) PRIMARY KEY, requires_web_hitl BOOLEAN)")

    with pytest.raises(RuntimeError, match="force rollback"):
        with engine.begin() as connection:
            migrate_0038_remove_agent_registry_requires_web_hitl(connection)
            raise RuntimeError("force rollback")

    with engine.connect() as connection:
        assert "requires_web_hitl" in _columns(connection, "agent_registry")
