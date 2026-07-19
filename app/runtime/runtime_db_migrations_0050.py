from __future__ import annotations

import json

from sqlalchemy.engine import Connection

from .runtime_db_base import utc_now


def migrate_0050_deduplicate_active_agent_test_runs(connection: Connection) -> None:
    """Keep one active run per exact Agent/change-set/commit target."""

    if not _table_exists(connection, "agent_test_runs"):
        return
    duplicate_groups = connection.exec_driver_sql(
        """
        SELECT agent_id, commit_sha, COALESCE(change_set_id, ''), COUNT(*)
        FROM agent_test_runs
        WHERE status IN ('queued', 'running')
        GROUP BY agent_id, commit_sha, COALESCE(change_set_id, '')
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for agent_id, commit_sha, change_set_key, _ in duplicate_groups:
        rows = connection.exec_driver_sql(
            """
            SELECT test_run_id
            FROM agent_test_runs
            WHERE agent_id = ?
              AND commit_sha = ?
              AND COALESCE(change_set_id, '') = ?
              AND status IN ('queued', 'running')
            ORDER BY created_at ASC, test_run_id ASC
            """,
            (agent_id, commit_sha, change_set_key),
        ).fetchall()
        for (test_run_id,) in rows[1:]:
            connection.exec_driver_sql(
                """
                UPDATE agent_test_runs
                SET status = 'interrupted', completed_at = ?, error_json = ?
                WHERE test_run_id = ? AND status IN ('queued', 'running')
                """,
                (
                    utc_now(),
                    json.dumps(
                        {
                            "error_code": "AGENT_TEST_RUN_DUPLICATE_RECONCILED",
                            "message": "A duplicate active test run was interrupted during schema migration.",
                        },
                        ensure_ascii=False,
                    ),
                    test_run_id,
                ),
            )
    connection.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_test_runs_active_target
        ON agent_test_runs (agent_id, commit_sha, COALESCE(change_set_id, ''))
        WHERE status IN ('queued', 'running')
        """
    )


def _table_exists(connection: Connection, table_name: str) -> bool:
    return connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).first() is not None
