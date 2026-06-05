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
