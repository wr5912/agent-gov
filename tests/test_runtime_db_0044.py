from __future__ import annotations

import json

from app.runtime.runtime_db import (
    AgentReleaseSourceClaimModel,
    SchemaMigration,
    make_engine,
    make_session_factory,
)
from app.runtime.runtime_db_migrations_0044 import migrate_0044_agent_release_source_claims
from sqlalchemy import create_engine


def _create_legacy_releases(connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE agent_releases (
            release_id VARCHAR(128) PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            change_set_id VARCHAR(128),
            payload_json JSON
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE agent_change_sets (
            change_set_id VARCHAR(128) PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            status VARCHAR(64) NOT NULL,
            payload_json JSON
        )
        """
    )
    AgentReleaseSourceClaimModel.__table__.create(connection)


def _insert_release(connection, *, release_id: str, change_set_id: str, source_improvement_id: str, created_at: str) -> None:
    connection.exec_driver_sql(
        "INSERT INTO agent_releases VALUES (?, ?, ?, ?, ?)",
        (
            release_id,
            "main-agent",
            created_at,
            change_set_id,
            json.dumps({"source_improvement_id": source_improvement_id}),
        ),
    )


def test_0044_backfills_one_durable_claim_for_historical_duplicate_releases(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}", future=True)
    with engine.begin() as connection:
        _create_legacy_releases(connection)
        _insert_release(
            connection,
            release_id="agr-first",
            change_set_id="agc-first",
            source_improvement_id="imp-shared",
            created_at="2026-07-01T00:00:00+00:00",
        )
        _insert_release(
            connection,
            release_id="agr-duplicate",
            change_set_id="agc-duplicate",
            source_improvement_id="imp-shared",
            created_at="2026-07-02T00:00:00+00:00",
        )
        _insert_release(
            connection,
            release_id="agr-other",
            change_set_id="agc-other",
            source_improvement_id="imp-other",
            created_at="2026-07-03T00:00:00+00:00",
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_change_sets VALUES (?, ?, ?, ?, ?)",
            (
                "agc-pending",
                "main-agent",
                "2026-07-04T00:00:00+00:00",
                "publishing",
                json.dumps(
                    {
                        "publication_intent": {
                            "release_id": "agr-pending",
                            "source_improvement_id": "imp-pending",
                            "started_at": "2026-07-04T00:00:01+00:00",
                        }
                    }
                ),
            ),
        )
        migrate_0044_agent_release_source_claims(connection)
        migrate_0044_agent_release_source_claims(connection)

    with engine.connect() as connection:
        claims = connection.exec_driver_sql(
            """
            SELECT source_improvement_id, change_set_id, release_id
            FROM agent_release_source_claims
            ORDER BY source_improvement_id
            """
        ).fetchall()

    assert claims == [
        ("imp-other", "agc-other", "agr-other"),
        ("imp-pending", "agc-pending", "agr-pending"),
        ("imp-shared", "agc-first", "agr-first"),
    ]


def test_0044_is_registered_and_fresh_schema_owns_claim_table(tmp_path) -> None:
    path = tmp_path / "fresh.sqlite3"
    factory = make_session_factory(path)

    with factory() as db:
        assert db.get(SchemaMigration, "0044_agent_release_source_claims") is not None
    with make_engine(path).connect() as connection:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_release_source_claims)")}

    assert columns >= {"agent_id", "source_improvement_id", "change_set_id", "release_id", "created_at"}
