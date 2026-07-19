from __future__ import annotations

import json

from app.runtime.runtime_db import SchemaMigration, make_engine, make_session_factory
from app.runtime.runtime_db_migrations_0046 import migrate_0046_remove_agent_registry_origin
from sqlalchemy import create_engine


def test_0046_drops_origin_and_cleans_provision_snapshot(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_registry (
                agent_id VARCHAR(128) PRIMARY KEY,
                origin VARCHAR(16),
                provision_previous_json JSON
            )
            """
        )
        connection.exec_driver_sql("CREATE INDEX ix_agent_registry_origin ON agent_registry (origin)")
        connection.exec_driver_sql(
            "INSERT INTO agent_registry VALUES (?, ?, ?)",
            (
                "legacy-agent",
                "seed",
                json.dumps({"name": "Legacy", "origin": "seed", "status": "active"}),
            ),
        )
        migrate_0046_remove_agent_registry_origin(connection)
        migrate_0046_remove_agent_registry_origin(connection)

    with engine.connect() as connection:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}
        raw_snapshot = connection.exec_driver_sql("SELECT provision_previous_json FROM agent_registry WHERE agent_id = 'legacy-agent'").scalar_one()
        indexes = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA index_list(agent_registry)")}

    assert "origin" not in columns
    assert json.loads(raw_snapshot) == {"name": "Legacy", "status": "active"}
    assert "ix_agent_registry_origin" not in indexes


def test_0046_is_registered_and_fresh_schema_has_no_origin(tmp_path) -> None:
    path = tmp_path / "fresh.sqlite3"
    factory = make_session_factory(path)

    with factory() as db:
        assert db.get(SchemaMigration, "0046_remove_agent_registry_origin") is not None
    with make_engine(path).connect() as connection:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}

    assert "origin" not in columns
