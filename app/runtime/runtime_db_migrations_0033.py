from __future__ import annotations

from sqlalchemy.engine import Connection

_IMPROVEMENT_STAGE_REPAIR_SQL = """
UPDATE improvement_items
SET improvement_stage = CASE
        WHEN __RELEASE_CLAUSE__ THEN 'release'
        WHEN EXISTS (
            SELECT 1 FROM __REGRESSION_TABLE__ AS r
            WHERE r.improvement_id = improvement_items.improvement_id
              AND TRIM(COALESCE(r.summary, '')) != ''
              AND EXISTS (
                SELECT 1 FROM json_each(COALESCE(r.__REGRESSION_JSON_COLUMN__, '[]')) AS regression_test
                WHERE TRIM(COALESCE(json_extract(regression_test.value, '$.__REGRESSION_REQUIRED_FIELD__'), '')) != ''
              )
        ) AND EXISTS (
            SELECT 1 FROM execution_records AS e
            WHERE e.improvement_id = improvement_items.improvement_id
              AND TRIM(COALESCE(e.summary, '')) != ''
              AND (
                (
                  TRIM(COALESCE(e.change_set_id, '')) != ''
                  AND TRIM(COALESCE(e.applied_agent_version_id, '')) != ''
                  AND COALESCE(e.applied_diff_json, '{}') NOT IN ('{}', 'null', '')
                )
                OR (
                  TRIM(COALESCE(e.agent_version, '')) != ''
                  AND COALESCE(e.changes_applied_json, '[]') NOT IN ('[]', 'null', '')
                )
              )
        ) THEN 'regression'
        WHEN EXISTS (
            SELECT 1 FROM execution_records AS e
            WHERE e.improvement_id = improvement_items.improvement_id
              AND TRIM(COALESCE(e.summary, '')) != ''
              AND (
                (
                  TRIM(COALESCE(e.change_set_id, '')) != ''
                  AND TRIM(COALESCE(e.applied_agent_version_id, '')) != ''
                  AND COALESCE(e.applied_diff_json, '{}') NOT IN ('{}', 'null', '')
                )
                OR (
                  TRIM(COALESCE(e.agent_version, '')) != ''
                  AND COALESCE(e.changes_applied_json, '[]') NOT IN ('[]', 'null', '')
                )
              )
        ) THEN 'execution'
        WHEN EXISTS (
            SELECT 1 FROM optimization_plans AS p
            WHERE p.improvement_id = improvement_items.improvement_id
              AND TRIM(COALESCE(p.summary, '')) != ''
              AND EXISTS (
                SELECT 1 FROM json_each(COALESCE(p.changes_json, '[]')) AS plan_change
                WHERE TRIM(COALESCE(json_extract(plan_change.value, '$.target'), '')) != ''
                  AND TRIM(COALESCE(json_extract(plan_change.value, '$.change'), '')) != ''
              )
        ) THEN 'optimization'
        WHEN EXISTS (
            SELECT 1 FROM attributions AS a
            WHERE a.improvement_id = improvement_items.improvement_id
              AND TRIM(COALESCE(a.summary, '')) != ''
        ) THEN 'attribution'
        WHEN EXISTS (
            SELECT 1 FROM normalized_feedbacks AS n
            WHERE n.improvement_id = improvement_items.improvement_id
              AND TRIM(COALESCE(n.problem, '')) != ''
        ) THEN 'triage'
        ELSE 'feedback_intake'
    END,
    improvement_status = CASE
        WHEN improvement_status = 'archived' THEN 'archived'
        WHEN __RELEASE_CLAUSE__ THEN 'done'
        ELSE 'active'
    END
"""


def migrate_0033_repair_improvement_stages_from_artifacts(connection: Connection) -> None:
    """Repair stage/status shells left by the removed stage-only automation."""
    required_tables = {
        "improvement_items",
        "normalized_feedbacks",
        "attributions",
        "optimization_plans",
        "execution_records",
    }
    if any(not _table_columns(connection, table) for table in required_tables):
        return
    regression_table = "regression_test_designs" if _table_columns(connection, "regression_test_designs") else "regression_assessments"
    regression_columns = _table_columns(connection, regression_table)
    if not regression_columns:
        return
    regression_json_column = "tests_json" if "tests_json" in regression_columns else "cases_json"
    regression_required_field = "test_code" if regression_json_column == "tests_json" else "prompt"
    release_clause = "0"
    if {"status", "payload_json"}.issubset(_table_columns(connection, "agent_releases")):
        release_clause = """
            EXISTS (
                SELECT 1 FROM agent_releases AS rel
                WHERE rel.status = 'published'
                  AND json_extract(rel.payload_json, '$.source_improvement_id') = improvement_items.improvement_id
            )
        """
    repair_sql = (
        _IMPROVEMENT_STAGE_REPAIR_SQL.replace("__RELEASE_CLAUSE__", release_clause)
        .replace("__REGRESSION_JSON_COLUMN__", regression_json_column)
        .replace("__REGRESSION_REQUIRED_FIELD__", regression_required_field)
    )
    connection.exec_driver_sql(repair_sql.replace("__REGRESSION_TABLE__", regression_table))


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}
