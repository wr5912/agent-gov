from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction


def migrate_0044_agent_release_source_claims(connection: Connection) -> None:
    """Backfill the durable source-publication fence from historical releases."""

    claim_columns = _table_columns(connection, "agent_release_source_claims")
    release_columns = _table_columns(connection, "agent_releases")
    change_set_columns = _table_columns(connection, "agent_change_sets")
    if not {"agent_id", "source_improvement_id", "change_set_id", "release_id", "created_at"} <= claim_columns:
        return

    begin_sqlite_write_transaction(connection)
    if {"agent_id", "change_set_id", "release_id", "created_at", "payload_json"} <= release_columns:
        _backfill_release_claims(connection)
    if {"agent_id", "change_set_id", "created_at", "status", "payload_json"} <= change_set_columns:
        _backfill_publication_intent_claims(connection)


def _backfill_release_claims(connection: Connection) -> None:
    rows = connection.exec_driver_sql(
        """
        SELECT
            COALESCE(NULLIF(agent_id, ''), 'main-agent') AS agent_id,
            json_extract(payload_json, '$.source_improvement_id') AS source_improvement_id,
            change_set_id,
            release_id,
            created_at
        FROM agent_releases
        WHERE change_set_id IS NOT NULL
          AND change_set_id != ''
          AND json_valid(COALESCE(payload_json, '{}'))
          AND NULLIF(json_extract(payload_json, '$.source_improvement_id'), '') IS NOT NULL
        ORDER BY created_at, release_id
        """
    ).fetchall()
    for agent_id, source_improvement_id, change_set_id, release_id, created_at in rows:
        connection.exec_driver_sql(
            """
            INSERT OR IGNORE INTO agent_release_source_claims (
                agent_id, source_improvement_id, change_set_id, release_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(agent_id),
                str(source_improvement_id),
                str(change_set_id),
                str(release_id),
                str(created_at),
            ),
        )


def _backfill_publication_intent_claims(connection: Connection) -> None:
    rows = connection.exec_driver_sql(
        """
        SELECT
            COALESCE(NULLIF(agent_id, ''), 'main-agent') AS agent_id,
            json_extract(payload_json, '$.publication_intent.source_improvement_id') AS source_improvement_id,
            change_set_id,
            json_extract(payload_json, '$.publication_intent.release_id') AS release_id,
            COALESCE(
                NULLIF(json_extract(payload_json, '$.publication_intent.started_at'), ''),
                created_at
            ) AS claim_created_at
        FROM agent_change_sets
        WHERE status IN ('publishing', 'published')
          AND json_valid(COALESCE(payload_json, '{}'))
          AND NULLIF(json_extract(payload_json, '$.publication_intent.source_improvement_id'), '') IS NOT NULL
          AND NULLIF(json_extract(payload_json, '$.publication_intent.release_id'), '') IS NOT NULL
        ORDER BY claim_created_at, change_set_id
        """
    ).fetchall()
    for agent_id, source_improvement_id, change_set_id, release_id, created_at in rows:
        connection.exec_driver_sql(
            """
            INSERT OR IGNORE INTO agent_release_source_claims (
                agent_id, source_improvement_id, change_set_id, release_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(agent_id),
                str(source_improvement_id),
                str(change_set_id),
                str(release_id),
                str(created_at),
            ),
        )


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")').fetchall()}
