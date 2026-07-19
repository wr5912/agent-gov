from __future__ import annotations

import json

from sqlalchemy.engine import Connection


def migrate_0049_rename_regression_test_design(connection: Connection) -> None:
    """Rename the active test-design artifact without keeping a dual schema."""

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS regression_test_designs (
            regression_test_design_id VARCHAR(128) NOT NULL PRIMARY KEY,
            improvement_id VARCHAR(128) NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            cases_json JSON NOT NULL,
            suggested_gate_thresholds_json JSON NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'draft',
            generated_by VARCHAR(32) NOT NULL DEFAULT 'heuristic',
            generation_trace_id VARCHAR(256) NOT NULL DEFAULT '',
            generation_trace_url VARCHAR(2048) NOT NULL DEFAULT '',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_regression_test_designs_improvement_id "
        "ON regression_test_designs (improvement_id)"
    )

    if _table_exists(connection, "regression_assessments"):
        existing = int(
            connection.exec_driver_sql("SELECT COUNT(*) FROM regression_test_designs").scalar_one()
        )
        legacy = int(
            connection.exec_driver_sql("SELECT COUNT(*) FROM regression_assessments").scalar_one()
        )
        if existing and legacy:
            raise RuntimeError(
                "regression_test_designs and regression_assessments both contain rows; refusing an ambiguous merge"
            )
        if legacy:
            connection.exec_driver_sql(
                """
                INSERT INTO regression_test_designs (
                    regression_test_design_id,
                    improvement_id,
                    summary,
                    cases_json,
                    suggested_gate_thresholds_json,
                    status,
                    generated_by,
                    generation_trace_id,
                    generation_trace_url,
                    created_at,
                    updated_at
                )
                SELECT
                    regression_assessment_id,
                    improvement_id,
                    COALESCE(summary, ''),
                    COALESCE(cases_json, '[]'),
                    COALESCE(suggested_gate_thresholds_json, '{}'),
                    COALESCE(status, 'draft'),
                    COALESCE(generated_by, 'heuristic'),
                    COALESCE(generation_trace_id, ''),
                    COALESCE(generation_trace_url, ''),
                    created_at,
                    updated_at
                FROM regression_assessments
                """
            )
        connection.exec_driver_sql("DROP TABLE regression_assessments")

    if _table_exists(connection, "agent_jobs") and "job_type" in _table_columns(connection, "agent_jobs"):
        connection.exec_driver_sql(
            "UPDATE agent_jobs SET job_type = 'regression_test_design' "
            "WHERE job_type = 'regression_assessment'"
        )
    _remove_legacy_change_set_test_payload(connection)


def _table_exists(connection: Connection, table: str) -> bool:
    return (
        connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def _remove_legacy_change_set_test_payload(connection: Connection) -> None:
    if not _table_exists(connection, "agent_change_sets") or "payload_json" not in _table_columns(
        connection, "agent_change_sets"
    ):
        return
    for change_set_id, raw_payload in connection.exec_driver_sql(
        "SELECT change_set_id, payload_json FROM agent_change_sets"
    ).fetchall():
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except ValueError:
                continue
        elif isinstance(raw_payload, dict):
            payload = dict(raw_payload)
        else:
            continue
        changed = False
        for key in ("latest_eval_run_id", "latest_eval_run", "regression_attempt_id"):
            if key in payload:
                payload.pop(key)
                changed = True
        if changed:
            connection.exec_driver_sql(
                "UPDATE agent_change_sets SET payload_json = ? WHERE change_set_id = ?",
                (json.dumps(payload, ensure_ascii=False), str(change_set_id)),
            )
