from __future__ import annotations

import json

from sqlalchemy.engine import Connection

from .runtime_db_base import utc_now

AGENT_JOB_COLUMNS_WITHOUT_OUTPUT_CONTRACT = (
    "job_id",
    "job_type",
    "scope_kind",
    "scope_id",
    "status",
    "profile_name",
    "created_at",
    "started_at",
    "completed_at",
    "input_path",
    "raw_output_path",
    "validated_output_path",
    "error_path",
    "runtime_version",
    "schema_version",
    "timeout_seconds",
    "retry_count",
    "profile_version_json",
    "input_json",
    "raw_output_json",
    "validated_output_json",
    "error_json",
)


def migrate_0006_remove_agent_job_output_contract_column(connection: Connection) -> None:
    if "output_schema_version" not in _table_columns(connection, "agent_jobs"):
        return
    connection.exec_driver_sql("ALTER TABLE agent_jobs RENAME TO agent_jobs_with_output_schema_version")
    _create_agent_jobs_table(connection)
    columns = ", ".join(AGENT_JOB_COLUMNS_WITHOUT_OUTPUT_CONTRACT)
    connection.exec_driver_sql(f"INSERT INTO agent_jobs ({columns}) SELECT {columns} FROM agent_jobs_with_output_schema_version")
    connection.exec_driver_sql("DROP TABLE agent_jobs_with_output_schema_version")
    _create_agent_jobs_indexes(connection)


def _create_agent_jobs_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE agent_jobs (
            job_id VARCHAR(128) NOT NULL PRIMARY KEY,
            job_type VARCHAR(64) NOT NULL,
            scope_kind VARCHAR(64) NOT NULL,
            scope_id VARCHAR(256) NOT NULL,
            status VARCHAR(64) NOT NULL,
            profile_name VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            started_at VARCHAR(64),
            completed_at VARCHAR(64),
            input_path VARCHAR(2048) NOT NULL,
            raw_output_path VARCHAR(2048) NOT NULL,
            validated_output_path VARCHAR(2048) NOT NULL,
            error_path VARCHAR(2048) NOT NULL,
            runtime_version VARCHAR(64) NOT NULL,
            schema_version VARCHAR(64) NOT NULL,
            timeout_seconds INTEGER,
            retry_count INTEGER,
            profile_version_json JSON,
            input_json JSON,
            raw_output_json JSON,
            validated_output_json JSON,
            error_json JSON
        )
        """
    )


def _create_agent_jobs_indexes(connection: Connection) -> None:
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_job_type ON agent_jobs (job_type)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_scope_kind ON agent_jobs (scope_kind)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_scope_id ON agent_jobs (scope_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_status ON agent_jobs (status)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_profile_name ON agent_jobs (profile_name)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_created_at ON agent_jobs (created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_type_status_created ON agent_jobs (job_type, status, created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_scope_type_created ON agent_jobs (scope_kind, scope_id, job_type, created_at)")


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def migrate_0007_agent_registry(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_registry (
            agent_id VARCHAR(128) NOT NULL PRIMARY KEY,
            name VARCHAR(256) NOT NULL,
            category VARCHAR(32) NOT NULL,
            workspace_dir VARCHAR(2048) NOT NULL,
            created_at VARCHAR(64) NOT NULL
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_registry_category ON agent_registry (category)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_registry_created_at ON agent_registry (created_at)")


def migrate_0008_feedback_signal_agent_id(connection: Connection) -> None:
    if "agent_id" not in _table_columns(connection, "feedback_signals"):
        connection.exec_driver_sql("ALTER TABLE feedback_signals ADD COLUMN agent_id VARCHAR(128)")
    connection.exec_driver_sql("UPDATE feedback_signals SET agent_id = 'main-agent' WHERE agent_id IS NULL")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_feedback_signals_agent_id ON feedback_signals (agent_id)")


def migrate_0009_agent_registry_status(connection: Connection) -> None:
    if "status" not in _table_columns(connection, "agent_registry"):
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN status VARCHAR(32)")
    connection.exec_driver_sql("UPDATE agent_registry SET status = 'active' WHERE status IS NULL")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_registry_status ON agent_registry (status)")


def migrate_0011_change_set_release_agent_id(connection: Connection) -> None:
    for table in ("agent_change_sets", "agent_releases"):
        if "agent_id" not in _table_columns(connection, table):
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN agent_id VARCHAR(128)")
        connection.exec_driver_sql(f"UPDATE {table} SET agent_id = 'main-agent' WHERE agent_id IS NULL")
        connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_agent_id ON {table} (agent_id)")


def migrate_0012_eval_run_agent_id(connection: Connection) -> None:
    if "agent_id" not in _table_columns(connection, "eval_runs"):
        connection.exec_driver_sql("ALTER TABLE eval_runs ADD COLUMN agent_id VARCHAR(128)")
    connection.exec_driver_sql("UPDATE eval_runs SET agent_id = 'main-agent' WHERE agent_id IS NULL")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_runs_agent_id ON eval_runs (agent_id)")


def migrate_0014_improvement_feedback_context(connection: Connection) -> None:
    columns = _table_columns(connection, "improvement_feedbacks")
    if not columns:
        return
    for column_name, ddl in {
        "agent_version_id": "VARCHAR(256) DEFAULT ''",
        "scenario": "VARCHAR(256) DEFAULT ''",
        "task_id": "VARCHAR(256) DEFAULT ''",
        "alert_id": "VARCHAR(256) DEFAULT ''",
        "case_id": "VARCHAR(256) DEFAULT ''",
    }.items():
        if column_name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE improvement_feedbacks ADD COLUMN {column_name} {ddl}")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_improvement_feedbacks_agent_version_id ON improvement_feedbacks (agent_version_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_improvement_feedbacks_scenario ON improvement_feedbacks (scenario)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_improvement_feedbacks_task_id ON improvement_feedbacks (task_id)")


def migrate_0015_improvement_content_generated_by(connection: Connection) -> None:
    """§17.5：归因/优化方案补 generated_by（governor / heuristic 来源标注）。已存在的旧表 ALTER 补列。"""
    for table in ("attributions", "optimization_plans"):
        columns = _table_columns(connection, table)
        if not columns:
            continue
        if "generated_by" not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN generated_by VARCHAR(32) DEFAULT 'heuristic'")


def migrate_0016_execution_application_binding(connection: Connection) -> None:
    """§17.5：执行记录补 generated_by + change_set_id + applied_agent_version_id + applied_diff_json（自动 apply 权威绑定）。"""
    columns = _table_columns(connection, "execution_records")
    if not columns:
        return
    for column_name, ddl in {
        "generated_by": "VARCHAR(32) DEFAULT 'heuristic'",
        "change_set_id": "VARCHAR(128) DEFAULT ''",
        "applied_agent_version_id": "VARCHAR(128) DEFAULT ''",
        "applied_diff_json": "JSON",
    }.items():
        if column_name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE execution_records ADD COLUMN {column_name} {ddl}")


def migrate_0017_regression_assessments(connection: Connection) -> None:
    """§17.5：回归保障评估表 regression_assessments（治理 Agent 生成回归用例候选）。新表 create_all 已建，此处仅幂等保障。"""
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS regression_assessments (
            regression_assessment_id VARCHAR(128) NOT NULL PRIMARY KEY,
            improvement_id VARCHAR(128),
            summary TEXT,
            cases_json JSON,
            status VARCHAR(32) DEFAULT 'draft',
            generated_by VARCHAR(32) DEFAULT 'heuristic',
            created_at VARCHAR(64),
            updated_at VARCHAR(64)
        )
        """
    )
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_regression_assessments_improvement_id ON regression_assessments (improvement_id)")


def migrate_0010_scenario_packs(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS scenario_packs (
            scenario_pack_id VARCHAR(128) NOT NULL PRIMARY KEY,
            name VARCHAR(256) NOT NULL,
            business_goal VARCHAR(2048) NOT NULL DEFAULT '',
            scope VARCHAR(2048) NOT NULL DEFAULT '',
            risk_level VARCHAR(32) NOT NULL DEFAULT 'medium',
            created_at VARCHAR(64) NOT NULL,
            payload_json JSON NOT NULL
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_scenario_packs_created_at ON scenario_packs (created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_scenario_packs_risk_level ON scenario_packs (risk_level)")


def migrate_0005_agent_governance(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_change_sets (
            change_set_id VARCHAR(128) NOT NULL PRIMARY KEY,
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            status VARCHAR(64) NOT NULL,
            execution_job_id VARCHAR(128),
            base_commit_sha VARCHAR(64) NOT NULL,
            candidate_commit_sha VARCHAR(64),
            branch_name VARCHAR(256) NOT NULL,
            worktree_path VARCHAR(2048) NOT NULL,
            payload_json JSON NOT NULL
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_created_at ON agent_change_sets (created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_updated_at ON agent_change_sets (updated_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_status ON agent_change_sets (status)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_execution_job_id ON agent_change_sets (execution_job_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_base_commit_sha ON agent_change_sets (base_commit_sha)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_candidate_commit_sha ON agent_change_sets (candidate_commit_sha)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_branch_name ON agent_change_sets (branch_name)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_status_updated ON agent_change_sets (status, updated_at)")
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_change_set_events (
            event_id VARCHAR(128) NOT NULL PRIMARY KEY,
            change_set_id VARCHAR(128) NOT NULL,
            action VARCHAR(64) NOT NULL,
            operator VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            before_json JSON NOT NULL,
            after_json JSON NOT NULL,
            FOREIGN KEY(change_set_id) REFERENCES agent_change_sets (change_set_id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_set_events_change_set_id ON agent_change_set_events (change_set_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_set_events_action ON agent_change_set_events (action)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_set_events_operator ON agent_change_set_events (operator)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_set_events_created_at ON agent_change_set_events (created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_set_events_change_created ON agent_change_set_events (change_set_id, created_at)")
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_releases (
            release_id VARCHAR(128) NOT NULL PRIMARY KEY,
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            status VARCHAR(64) NOT NULL,
            tag_name VARCHAR(256) NOT NULL,
            commit_sha VARCHAR(64) NOT NULL,
            change_set_id VARCHAR(128),
            rollback_of_release_id VARCHAR(128),
            archive_path VARCHAR(2048),
            payload_json JSON NOT NULL
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_created_at ON agent_releases (created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_updated_at ON agent_releases (updated_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_status ON agent_releases (status)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_tag_name ON agent_releases (tag_name)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_commit_sha ON agent_releases (commit_sha)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_change_set_id ON agent_releases (change_set_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_rollback_of_release_id ON agent_releases (rollback_of_release_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_releases_status_created ON agent_releases (status, created_at)")


def migrate_0018_agent_registry_origin_tombstone(connection: Connection) -> None:
    """#26：业务 Agent 注册表加 origin（seed=声明式基线禁删 / user=用户创建可删）+ deleted_at（用户删除 tombstone，
    重启不被 discover_seeded 复活）。已存在行默认 origin='user'，seed agent 的 origin 由启动 sync 按 seed 目录校正。"""
    columns = _table_columns(connection, "agent_registry")
    if not columns:
        return
    if "origin" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN origin VARCHAR(16) DEFAULT 'user'")
    connection.exec_driver_sql("UPDATE agent_registry SET origin = 'user' WHERE origin IS NULL")
    if "deleted_at" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN deleted_at VARCHAR(64)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_registry_origin ON agent_registry (origin)")


def migrate_0019_improvement_detail_columns(connection: Connection) -> None:
    """四阶段详情表补齐 UI/API 新增字段，避免旧运行卷在归因/方案/执行/回归写入时缺列 500。"""
    for table, columns_to_add in {
        "attributions": {
            "counter_evidence_json": "JSON",
            "uncertainty_factors_json": "JSON",
            "verification_suggestions_json": "JSON",
        },
        "optimization_plans": {
            "risk_level": "VARCHAR(32) DEFAULT ''",
        },
        "execution_records": {
            "risk_level": "VARCHAR(32) DEFAULT ''",
            "rollback_strategy": "TEXT DEFAULT ''",
            "rollback_instructions_json": "JSON",
        },
        "regression_assessments": {
            "suggested_gate_thresholds_json": "JSON",
        },
    }.items():
        existing_columns = _table_columns(connection, table)
        if not existing_columns:
            continue
        for column_name, ddl in columns_to_add.items():
            if column_name not in existing_columns:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column_name} {ddl}")


def migrate_0020_claude_user_input_requests(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS claude_user_input_requests (
            request_id VARCHAR(128) NOT NULL PRIMARY KEY,
            decision_token_hash VARCHAR(128) NOT NULL,
            business_agent_id VARCHAR(128) NOT NULL,
            run_id VARCHAR(128) NOT NULL,
            api_session_id VARCHAR(128) NOT NULL,
            sdk_session_id VARCHAR(256),
            tool_use_id VARCHAR(128),
            sdk_subagent_id VARCHAR(128),
            request_type VARCHAR(32) NOT NULL,
            tool_name VARCHAR(256) NOT NULL,
            redacted_input_json JSON,
            context_json JSON,
            risk_json JSON,
            status VARCHAR(32) NOT NULL,
            decision VARCHAR(32),
            decision_payload_json JSON,
            decided_by VARCHAR(128),
            created_at VARCHAR(64) NOT NULL,
            expires_at VARCHAR(64) NOT NULL,
            resolved_at VARCHAR(64)
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_business_agent_id ON claude_user_input_requests (business_agent_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_run_id ON claude_user_input_requests (run_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_api_session_id ON claude_user_input_requests (api_session_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_tool_use_id ON claude_user_input_requests (tool_use_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_request_type ON claude_user_input_requests (request_type)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_tool_name ON claude_user_input_requests (tool_name)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_status ON claude_user_input_requests (status)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_decision ON claude_user_input_requests (decision)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_created_at ON claude_user_input_requests (created_at)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_requests_expires_at ON claude_user_input_requests (expires_at)")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_claude_user_input_agent_status ON claude_user_input_requests (business_agent_id, status, created_at)"
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claude_user_input_run_status ON claude_user_input_requests (run_id, status, created_at)")


def migrate_0021_improvement_generation_trace_refs(connection: Connection) -> None:
    """四阶段治理内容记录保存 Langfuse trace ref，旧卷幂等补列。"""
    for table in ("attributions", "optimization_plans", "execution_records", "regression_assessments"):
        existing_columns = _table_columns(connection, table)
        if not existing_columns:
            continue
        if "generation_trace_id" not in existing_columns:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN generation_trace_id VARCHAR(256) DEFAULT ''")
        if "generation_trace_url" not in existing_columns:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN generation_trace_url VARCHAR(2048) DEFAULT ''")


def migrate_0026_normalized_feedback_generation_refs(connection: Connection) -> None:
    """NormalizedFeedback 保存 generated_by + Langfuse trace ref（反馈整理生成 provenance），旧卷幂等补列。"""
    existing_columns = _table_columns(connection, "normalized_feedbacks")
    if not existing_columns:
        return
    if "generated_by" not in existing_columns:
        connection.exec_driver_sql("ALTER TABLE normalized_feedbacks ADD COLUMN generated_by VARCHAR(32) DEFAULT 'heuristic'")
    if "generation_trace_id" not in existing_columns:
        connection.exec_driver_sql("ALTER TABLE normalized_feedbacks ADD COLUMN generation_trace_id VARCHAR(256) DEFAULT ''")
    if "generation_trace_url" not in existing_columns:
        connection.exec_driver_sql("ALTER TABLE normalized_feedbacks ADD COLUMN generation_trace_url VARCHAR(2048) DEFAULT ''")


def migrate_0022_remove_legacy_batch_optimization_chain(connection: Connection) -> None:
    """Remove the replaced batch/task/proposal/external-governance optimization chain tables."""
    for table in (
        "regression_gate_overrides",
        "regression_impact_analyses",
        "regression_plans",
        "execution_applications",
        "execution_compensations",
        "external_notifications",
        "external_governance_items",
        "proposal_reviews",
        "optimization_proposals",
        "optimization_tasks",
        "feedback_optimization_batches",
    ):
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")


def migrate_0023_eval_case_targeted_regression_layer(connection: Connection) -> None:
    """Rename old eval-case asset layer value batch_specific to targeted_regression."""
    columns = _table_columns(connection, "eval_cases")
    if "asset_layer" not in columns:
        return
    connection.exec_driver_sql("UPDATE eval_cases SET asset_layer = 'targeted_regression' WHERE asset_layer = 'batch_specific'")


def migrate_0024_feedback_case_agent_id(connection: Connection) -> None:
    """Persist feedback case ownership for per-business-agent filtering and isolation."""
    case_columns = _table_columns(connection, "feedback_cases")
    if not case_columns:
        return
    if "agent_id" not in case_columns:
        connection.exec_driver_sql("ALTER TABLE feedback_cases ADD COLUMN agent_id VARCHAR(128) DEFAULT 'main-agent'")
    connection.exec_driver_sql("UPDATE feedback_cases SET agent_id = 'main-agent' WHERE agent_id IS NULL OR agent_id = ''")
    signal_columns = _table_columns(connection, "feedback_signals")
    if {"signal_id", "agent_id"}.issubset(signal_columns) and "signal_ids_json" in _table_columns(connection, "feedback_cases"):
        rows = connection.exec_driver_sql("SELECT feedback_case_id, signal_ids_json FROM feedback_cases").fetchall()
        for feedback_case_id, signal_ids_json in rows:
            signal_ids = _json_string_list(signal_ids_json)
            if not signal_ids:
                continue
            placeholders = ",".join("?" for _ in signal_ids)
            signal_rows = connection.exec_driver_sql(
                f"SELECT DISTINCT agent_id FROM feedback_signals WHERE signal_id IN ({placeholders}) AND agent_id IS NOT NULL AND agent_id != ''",
                tuple(signal_ids),
            ).fetchall()
            agent_ids = sorted({str(row[0]) for row in signal_rows if row[0]})
            if len(agent_ids) == 1:
                connection.exec_driver_sql(
                    "UPDATE feedback_cases SET agent_id = ? WHERE feedback_case_id = ?",
                    (agent_ids[0], feedback_case_id),
                )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_feedback_cases_agent_id ON feedback_cases (agent_id)")


def migrate_0025_agent_governance_legacy_paths(connection: Connection) -> None:
    """Rewrite legacy main-agent governance FS paths stored in SQLite rows."""
    replacements = (
        (
            "agent_change_sets",
            "worktree_path",
            "/data/agent-governance/worktrees",
            "/data/business-agents/main-agent/version/worktrees",
        ),
        (
            "agent_releases",
            "archive_path",
            "/data/agent-governance/releases",
            "/data/business-agents/main-agent/version/releases",
        ),
    )
    for table, column, old_root, new_root in replacements:
        if column not in _table_columns(connection, table):
            continue
        connection.exec_driver_sql(
            f"UPDATE {table} SET {column} = REPLACE({column}, ?, ?) WHERE {column} LIKE ?",
            (old_root, new_root, f"{old_root}%"),
        )


def migrate_0027_agent_registry_requires_web_hitl(connection: Connection) -> None:
    """部署契约：业务 Agent 注册表加 requires_web_hitl（agent.yaml 声明其执行能力依赖 web HITL）。

    已存在行默认 0；启动 sync 按 profile.requires_web_hitl（读 workspace/agent.yaml）幂等校正。
    """
    if "requires_web_hitl" not in _table_columns(connection, "agent_registry"):
        connection.exec_driver_sql("ALTER TABLE agent_registry ADD COLUMN requires_web_hitl BOOLEAN DEFAULT 0")
    connection.exec_driver_sql("UPDATE agent_registry SET requires_web_hitl = 0 WHERE requires_web_hitl IS NULL")


def migrate_0028_remove_improvement_automation_policy(connection: Connection) -> None:
    """Delete the obsolete stage-only automation policy table."""
    connection.exec_driver_sql("DROP TABLE IF EXISTS automation_policies")


_IMPROVEMENT_STAGE_REPAIR_SQL = """
UPDATE improvement_items
SET improvement_stage = CASE
        WHEN __RELEASE_CLAUSE__ THEN 'release'
        WHEN EXISTS (
            SELECT 1 FROM regression_assessments AS r
            WHERE r.improvement_id = improvement_items.improvement_id
              AND TRIM(COALESCE(r.summary, '')) != ''
              AND EXISTS (
                SELECT 1 FROM json_each(COALESCE(r.cases_json, '[]')) AS regression_case
                WHERE TRIM(COALESCE(json_extract(regression_case.value, '$.prompt'), '')) != ''
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
        "regression_assessments",
    }
    if any(not _table_columns(connection, table) for table in required_tables):
        return
    release_clause = "0"
    if {"status", "payload_json"}.issubset(_table_columns(connection, "agent_releases")):
        release_clause = """
            EXISTS (
                SELECT 1 FROM agent_releases AS rel
                WHERE rel.status = 'published'
                  AND json_extract(rel.payload_json, '$.source_improvement_id') = improvement_items.improvement_id
            )
        """
    connection.exec_driver_sql(_IMPROVEMENT_STAGE_REPAIR_SQL.replace("__RELEASE_CLAUSE__", release_clause))


def migrate_0030_improvement_execution_intents(connection: Connection) -> None:
    """补齐执行 intent/fencing，并把 improvement link 收口为幂等身份引用。"""
    execution_columns = _table_columns(connection, "execution_records")
    for column_name, ddl in {
        "base_commit_sha": "VARCHAR(64) DEFAULT ''",
        "source_optimization_plan_id": "VARCHAR(128) DEFAULT ''",
        "source_optimization_plan_updated_at": "VARCHAR(64) DEFAULT ''",
        "source_attribution_id": "VARCHAR(128) DEFAULT ''",
        "source_attribution_updated_at": "VARCHAR(64) DEFAULT ''",
        "claim_token": "VARCHAR(128) DEFAULT ''",
        "claim_generation": "INTEGER DEFAULT 0",
        "claim_expires_at": "VARCHAR(64) DEFAULT ''",
    }.items():
        if execution_columns and column_name not in execution_columns:
            connection.exec_driver_sql(f"ALTER TABLE execution_records ADD COLUMN {column_name} {ddl}")
    if _table_columns(connection, "improvement_links"):
        connection.exec_driver_sql(
            """
            DELETE FROM improvement_links
            WHERE rowid NOT IN (
                SELECT MIN(rowid)
                FROM improvement_links
                GROUP BY improvement_id, kind, ref_id
            )
            """
        )
        connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_improvement_links_identity ON improvement_links (improvement_id, kind, ref_id)")


def migrate_0032_improvement_execution_source_revisions(connection: Connection) -> None:
    """Add source revisions separately for databases that already applied migration 0030."""
    execution_columns = _table_columns(connection, "execution_records")
    for column_name, ddl in {
        "source_optimization_plan_id": "VARCHAR(128) DEFAULT ''",
        "source_optimization_plan_updated_at": "VARCHAR(64) DEFAULT ''",
        "source_attribution_id": "VARCHAR(128) DEFAULT ''",
        "source_attribution_updated_at": "VARCHAR(64) DEFAULT ''",
    }.items():
        if execution_columns and column_name not in execution_columns:
            connection.exec_driver_sql(f"ALTER TABLE execution_records ADD COLUMN {column_name} {ddl}")


def _ensure_feedback_case_assignment_tables(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS improvement_feedback_case_assignments (
            feedback_case_id VARCHAR(128) PRIMARY KEY,
            improvement_id VARCHAR(128) NOT NULL,
            feedback_id VARCHAR(128) NOT NULL UNIQUE,
            agent_id VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS improvement_feedback_case_assignment_conflicts (
            feedback_id VARCHAR(128) PRIMARY KEY,
            feedback_case_id VARCHAR(128) NOT NULL,
            improvement_id VARCHAR(128) NOT NULL,
            agent_id VARCHAR(128) NOT NULL,
            detected_at VARCHAR(64) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_improvement_feedback_case_assignments_improvement_id ON improvement_feedback_case_assignments (improvement_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_improvement_feedback_case_assignments_agent_id ON improvement_feedback_case_assignments (agent_id)"
    )


def _repair_feedback_case_assignment_conflicts(connection: Connection) -> bool:
    feedback_columns = _table_columns(connection, "improvement_feedbacks")
    required_feedback = {"feedback_id", "improvement_id", "agent_id", "source", "case_id", "created_at"}
    if not required_feedback.issubset(feedback_columns):
        return False
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO improvement_feedback_case_assignments
            (feedback_case_id, improvement_id, feedback_id, agent_id, created_at)
        SELECT case_id, improvement_id, feedback_id, agent_id, created_at
        FROM improvement_feedbacks
        WHERE source = 'feedback_inbox' AND case_id IS NOT NULL AND case_id != ''
        ORDER BY created_at ASC, feedback_id ASC
        """
    )
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO improvement_feedback_case_assignment_conflicts
            (feedback_id, feedback_case_id, improvement_id, agent_id, detected_at)
        SELECT f.feedback_id, f.case_id, f.improvement_id, f.agent_id, ?
        FROM improvement_feedbacks AS f
        LEFT JOIN improvement_feedback_case_assignments AS a ON a.feedback_id = f.feedback_id
        WHERE f.source = 'feedback_inbox' AND f.case_id IS NOT NULL AND f.case_id != ''
          AND a.feedback_id IS NULL
        """,
        (utc_now(),),
    )
    connection.exec_driver_sql(
        """
        UPDATE improvement_feedbacks
        SET source = 'feedback_inbox_conflict_snapshot', status = 'standalone', case_id = ''
        WHERE feedback_id IN (SELECT feedback_id FROM improvement_feedback_case_assignment_conflicts)
        """
    )
    return True


def _rebuild_feedback_case_item_refs(connection: Connection) -> None:
    item_columns = _table_columns(connection, "improvement_items")
    if not {"improvement_id", "source_feedback_refs_json"}.issubset(item_columns):
        return
    assignments = connection.exec_driver_sql("SELECT feedback_case_id, improvement_id FROM improvement_feedback_case_assignments").fetchall()
    owner_by_case = {str(case_id): str(improvement_id) for case_id, improvement_id in assignments}
    assigned_case_ids = set(owner_by_case)
    rows = connection.exec_driver_sql("SELECT improvement_id, source_feedback_refs_json FROM improvement_items").fetchall()
    for improvement_id, raw_refs in rows:
        refs = [ref for ref in _json_string_list(raw_refs) if ref not in assigned_case_ids]
        refs.extend(case_id for case_id, owner_id in owner_by_case.items() if owner_id == str(improvement_id) and case_id not in refs)
        connection.exec_driver_sql(
            "UPDATE improvement_items SET source_feedback_refs_json = ? WHERE improvement_id = ?",
            (json.dumps(refs, ensure_ascii=False), improvement_id),
        )


def migrate_0031_feedback_case_assignments(connection: Connection) -> None:
    """Backfill unique FeedbackCase ownership and rebuild its item-ref projection."""
    _ensure_feedback_case_assignment_tables(connection)
    if _repair_feedback_case_assignment_conflicts(connection):
        _rebuild_feedback_case_item_refs(connection)


def migrate_0034_repair_feedback_case_assignments(connection: Connection) -> None:
    """Re-run the idempotent ownership repair for databases that recorded the initial 0031."""
    migrate_0031_feedback_case_assignments(connection)


def migrate_0035_session_active_run_lease(connection: Connection) -> None:
    """Fence session deletion and concurrent turns while an SDK query is active."""
    session_columns = _table_columns(connection, "sessions")
    for column_name, ddl in {
        "active_run_id": "VARCHAR(128)",
        "active_run_expires_at": "VARCHAR(64)",
    }.items():
        if session_columns and column_name not in session_columns:
            connection.exec_driver_sql(f"ALTER TABLE sessions ADD COLUMN {column_name} {ddl}")
    if session_columns:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_sessions_active_run_id ON sessions (active_run_id)"
        )


def migrate_0029_agent_release_tag_claims(connection: Connection) -> None:
    """Persist one release-tag owner per Agent while preserving conflicting legacy rows for audit."""
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_release_tag_claims (
            agent_id VARCHAR(128) NOT NULL,
            tag_name VARCHAR(256) NOT NULL,
            change_set_id VARCHAR(128) NOT NULL,
            release_id VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            PRIMARY KEY (agent_id, tag_name)
        )
        """
    )
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_release_tag_claims_change_set ON agent_release_tag_claims (change_set_id)")
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_release_tag_claims_release ON agent_release_tag_claims (release_id)")
    release_columns = _table_columns(connection, "agent_releases")
    required = {"agent_id", "tag_name", "change_set_id", "release_id", "created_at"}
    if not required.issubset(release_columns):
        return
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO agent_release_tag_claims
            (agent_id, tag_name, change_set_id, release_id, created_at)
        SELECT
            COALESCE(NULLIF(agent_id, ''), 'main-agent'),
            tag_name,
            change_set_id,
            release_id,
            created_at
        FROM agent_releases
        WHERE tag_name IS NOT NULL AND tag_name != ''
          AND change_set_id IS NOT NULL AND change_set_id != ''
        ORDER BY created_at ASC, release_id ASC
        """
    )


def _json_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]
