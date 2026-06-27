from __future__ import annotations

from sqlalchemy.engine import Connection


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
    connection.exec_driver_sql(
        f"INSERT INTO agent_jobs ({columns}) SELECT {columns} FROM agent_jobs_with_output_schema_version"
    )
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


def migrate_0013_optimization_task_agent_id(connection: Connection) -> None:
    if "agent_id" not in _table_columns(connection, "optimization_tasks"):
        connection.exec_driver_sql("ALTER TABLE optimization_tasks ADD COLUMN agent_id VARCHAR(128)")
    connection.exec_driver_sql("UPDATE optimization_tasks SET agent_id = 'main-agent' WHERE agent_id IS NULL")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_optimization_tasks_agent_id ON optimization_tasks (agent_id)"
    )


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
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_improvement_feedbacks_agent_version_id "
        "ON improvement_feedbacks (agent_version_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_improvement_feedbacks_scenario ON improvement_feedbacks (scenario)"
    )
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
            optimization_task_id VARCHAR(128),
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
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_optimization_task_id ON agent_change_sets (optimization_task_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_execution_job_id ON agent_change_sets (execution_job_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_base_commit_sha ON agent_change_sets (base_commit_sha)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_candidate_commit_sha ON agent_change_sets (candidate_commit_sha)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_branch_name ON agent_change_sets (branch_name)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_change_sets_task_created ON agent_change_sets (optimization_task_id, created_at)")
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
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_change_set_events_change_created ON agent_change_set_events (change_set_id, created_at)"
    )
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
