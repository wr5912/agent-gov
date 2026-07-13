from __future__ import annotations

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction, utc_now
from .runtime_db_migrations_0036_feedback_cases import migrate_feedback_case_sources


def migrate_0036_agent_maintenance_feedback_and_session_reconciliation(connection: Connection) -> None:
    """Add durable admission, recovery, ownership, datasets, and SDK turn reconciliation."""
    begin_sqlite_write_transaction(connection)
    _migrate_session_reconciliation(connection)
    _migrate_agent_maintenance(connection)
    migrate_feedback_case_sources(connection)
    _migrate_test_datasets(connection)


def _migrate_session_reconciliation(connection: Connection) -> None:
    session_columns = _table_columns(connection, "sessions")
    for column_name, ddl in {
        "active_run_generation": "INTEGER NOT NULL DEFAULT 0",
        "sdk_project_key": "VARCHAR(256)",
        "sdk_store_ready_at": "VARCHAR(64)",
        "sdk_store_migration_error": "TEXT",
    }.items():
        if session_columns and column_name not in session_columns:
            connection.exec_driver_sql(f"ALTER TABLE sessions ADD COLUMN {column_name} {ddl}")
    if session_columns:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS session_turn_intents (
                run_id VARCHAR(128) NOT NULL PRIMARY KEY,
                session_id VARCHAR(128) NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                agent_id VARCHAR(128) NOT NULL,
                source_sdk_session_id VARCHAR(256),
                attempted_sdk_session_id VARCHAR(256) NOT NULL,
                sdk_project_key VARCHAR(256) NOT NULL,
                base_turns INTEGER NOT NULL,
                status VARCHAR(32) NOT NULL,
                request_json JSON NOT NULL DEFAULT '{}',
                error_json JSON NOT NULL DEFAULT '{}',
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                completed_at VARCHAR(64)
            )
            """
        )
        for column_name in (
            "session_id",
            "agent_id",
            "attempted_sdk_session_id",
            "sdk_project_key",
            "status",
            "created_at",
            "updated_at",
        ):
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_session_turn_intents_{column_name} ON session_turn_intents ({column_name})")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_session_turn_intents_one_running ON session_turn_intents (session_id) WHERE status = 'running'"
        )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS sdk_session_entries (
            entry_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            project_key VARCHAR(256) NOT NULL,
            sdk_session_id VARCHAR(256) NOT NULL,
            subpath VARCHAR(1024) NOT NULL DEFAULT '',
            entry_uuid VARCHAR(256),
            entry_json JSON NOT NULL,
            origin_run_id VARCHAR(128),
            committed_at VARCHAR(64),
            discarded_at VARCHAR(64),
            CONSTRAINT ck_sdk_session_entries_single_terminal_state
                CHECK (NOT (committed_at IS NOT NULL AND discarded_at IS NOT NULL))
        )
        """
    )
    for column_name in (
        "project_key",
        "sdk_session_id",
        "origin_run_id",
        "committed_at",
        "discarded_at",
    ):
        connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_sdk_session_entries_{column_name} ON sdk_session_entries ({column_name})")
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_sdk_session_entries_live_uuid "
        "ON sdk_session_entries (project_key, sdk_session_id, subpath, entry_uuid) "
        "WHERE entry_uuid IS NOT NULL AND discarded_at IS NULL"
    )


def _migrate_agent_maintenance(connection: Connection) -> None:
    _create_agent_admission_table(connection)
    _create_agent_release_operation_table(connection)
    _create_worktree_cleanup_task_table(connection)


def _create_agent_admission_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_admission_states (
            agent_id VARCHAR(128) NOT NULL PRIMARY KEY,
            generation INTEGER NOT NULL DEFAULT 0,
            maintenance_token VARCHAR(128),
            maintenance_generation INTEGER NOT NULL DEFAULT 0,
            maintenance_kind VARCHAR(64),
            maintenance_owner_id VARCHAR(256),
            maintenance_expires_at VARCHAR(64),
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )
        """
    )
    _create_column_indexes(
        connection,
        "agent_admission_states",
        (
            "maintenance_token",
            "maintenance_kind",
            "maintenance_owner_id",
            "maintenance_expires_at",
            "created_at",
            "updated_at",
        ),
    )


def _create_agent_release_operation_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_release_operations (
            operation_id VARCHAR(128) NOT NULL PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            release_id VARCHAR(128) NOT NULL REFERENCES agent_releases(release_id),
            operation_kind VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            expected_head_sha VARCHAR(64) NOT NULL,
            target_commit_sha VARCHAR(64) NOT NULL,
            release_expected_status VARCHAR(64) NOT NULL,
            release_expected_updated_at VARCHAR(64) NOT NULL,
            claim_token VARCHAR(128),
            claim_generation INTEGER NOT NULL DEFAULT 0,
            claim_expires_at VARCHAR(64),
            operator VARCHAR(128) NOT NULL,
            note TEXT,
            previous_head_sha VARCHAR(64),
            observed_head_sha VARCHAR(64),
            result_json JSON NOT NULL DEFAULT '{}',
            error_json JSON NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64)
        )
        """
    )
    _create_column_indexes(
        connection,
        "agent_release_operations",
        (
            "agent_id",
            "release_id",
            "operation_kind",
            "status",
            "claim_token",
            "claim_expires_at",
            "created_at",
            "updated_at",
        ),
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_release_operations_identity ON agent_release_operations (operation_kind, release_id, expected_head_sha)"
    )


def _create_worktree_cleanup_task_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_worktree_cleanup_tasks (
            change_set_id VARCHAR(128) NOT NULL PRIMARY KEY
                REFERENCES agent_change_sets(change_set_id) ON DELETE CASCADE,
            agent_id VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL,
            delete_branch BOOLEAN NOT NULL DEFAULT 1,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            claim_token VARCHAR(128),
            claim_generation INTEGER NOT NULL DEFAULT 0,
            claim_expires_at VARCHAR(64),
            next_retry_at VARCHAR(64),
            last_error_json JSON NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64)
        )
        """
    )
    _create_column_indexes(
        connection,
        "agent_worktree_cleanup_tasks",
        (
            "agent_id",
            "status",
            "claim_token",
            "claim_expires_at",
            "next_retry_at",
            "created_at",
            "updated_at",
        ),
    )


def _create_column_indexes(
    connection: Connection,
    table_name: str,
    columns: tuple[str, ...],
) -> None:
    for column_name in columns:
        connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_{column_name} ON {table_name} ({column_name})")


def _migrate_test_datasets(connection: Connection) -> None:
    _create_test_dataset_table(connection)
    _migrate_test_dataset_owner_columns(connection)
    _create_test_dataset_case_table(connection)
    _create_test_dataset_revision_table(connection)
    _migrate_eval_run_dataset_reference(connection)
    _archive_legacy_test_dataset_assets(connection)


def _create_test_dataset_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS test_datasets (
            dataset_id VARCHAR(128) NOT NULL PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            owner_kind VARCHAR(32) NOT NULL DEFAULT 'business_agent',
            owner_id VARCHAR(128) NOT NULL,
            source_improvement_id VARCHAR(128) NOT NULL,
            name VARCHAR(512) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            scope VARCHAR(512) NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            lifecycle_state VARCHAR(32) NOT NULL
                CHECK (lifecycle_state IN ('draft', 'active', 'evaluating', 'deprecated', 'archived')),
            source_regression_assessment_id VARCHAR(128),
            source_regression_assessment_updated_at VARCHAR(64),
            source_normalized_feedback_id VARCHAR(128),
            source_normalized_feedback_updated_at VARCHAR(64),
            source_attribution_id VARCHAR(128),
            source_attribution_updated_at VARCHAR(64),
            source_optimization_plan_id VARCHAR(128),
            source_optimization_plan_updated_at VARCHAR(64),
            source_execution_id VARCHAR(128),
            source_execution_updated_at VARCHAR(64),
            baseline_agent_version_id VARCHAR(256),
            candidate_agent_version_id VARCHAR(256),
            source_feedback_ids_json JSON NOT NULL DEFAULT '[]',
            quality_tags_json JSON NOT NULL DEFAULT '[]',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )
        """
    )


def _migrate_test_dataset_owner_columns(connection: Connection) -> None:
    columns = _table_columns(connection, "test_datasets")
    if "owner_kind" not in columns:
        connection.exec_driver_sql("ALTER TABLE test_datasets ADD COLUMN owner_kind VARCHAR(32) NOT NULL DEFAULT 'business_agent'")
    if "owner_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE test_datasets ADD COLUMN owner_id VARCHAR(128) NOT NULL DEFAULT ''")
    connection.exec_driver_sql("UPDATE test_datasets SET owner_id = agent_id WHERE owner_id IS NULL OR owner_id = ''")
    _create_column_indexes(
        connection,
        "test_datasets",
        (
            "agent_id",
            "owner_kind",
            "owner_id",
            "source_improvement_id",
            "lifecycle_state",
            "created_at",
            "updated_at",
        ),
    )


def _create_test_dataset_case_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS test_dataset_cases (
            case_id VARCHAR(128) NOT NULL PRIMARY KEY,
            dataset_id VARCHAR(128) NOT NULL
                REFERENCES test_datasets(dataset_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            expected_behavior TEXT NOT NULL,
            checkpoints_json JSON NOT NULL DEFAULT '[]'
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_test_dataset_cases_dataset_id ON test_dataset_cases (dataset_id)")
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_test_dataset_cases_position ON test_dataset_cases (dataset_id, position)")


def _create_test_dataset_revision_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS test_dataset_revisions (
            revision_id VARCHAR(128) NOT NULL PRIMARY KEY,
            dataset_id VARCHAR(128) NOT NULL
                REFERENCES test_datasets(dataset_id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK (revision > 0),
            previous_lifecycle_state VARCHAR(32),
            lifecycle_state VARCHAR(32) NOT NULL
                CHECK (lifecycle_state IN ('draft', 'active', 'evaluating', 'deprecated', 'archived')),
            operator VARCHAR(128) NOT NULL,
            reason VARCHAR(2048) NOT NULL,
            before_json JSON NOT NULL DEFAULT '{}',
            after_json JSON NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL
        )
        """
    )
    _create_column_indexes(
        connection,
        "test_dataset_revisions",
        ("dataset_id", "created_at"),
    )
    connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_test_dataset_revisions_dataset_revision ON test_dataset_revisions (dataset_id, revision)")
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO test_dataset_revisions (
            revision_id, dataset_id, revision, previous_lifecycle_state, lifecycle_state,
            operator, reason, before_json, after_json, created_at
        )
        SELECT dataset_id || ':revision:' || revision, dataset_id, revision, NULL,
               lifecycle_state, 'migration', '0036 existing dataset revision backfill',
               '{}', json_object(
                   'dataset_id', dataset_id,
                   'agent_id', agent_id,
                   'owner_kind', owner_kind,
                   'owner_id', owner_id,
                   'revision', revision,
                   'lifecycle_state', lifecycle_state
               ), created_at
        FROM test_datasets
        """
    )


def _migrate_eval_run_dataset_reference(connection: Connection) -> None:
    columns = _table_columns(connection, "eval_runs")
    if columns and "dataset_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE eval_runs ADD COLUMN dataset_id VARCHAR(128)")
    if columns:
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_runs_dataset_id ON eval_runs (dataset_id)")


def _archive_legacy_test_dataset_assets(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS archived_test_dataset_assets (
            legacy_asset_id VARCHAR(128) NOT NULL PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            title VARCHAR(512) NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            source_improvement_id VARCHAR(128) NOT NULL DEFAULT '',
            inherited_from VARCHAR(128) NOT NULL DEFAULT '',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            archived_at VARCHAR(64) NOT NULL,
            reason VARCHAR(512) NOT NULL
        )
        """
    )
    _create_column_indexes(
        connection,
        "archived_test_dataset_assets",
        ("agent_id", "source_improvement_id", "archived_at"),
    )
    required = {
        "asset_id",
        "agent_id",
        "asset_type",
        "title",
        "body",
        "source_improvement_id",
        "inherited_from",
        "created_at",
        "updated_at",
    }
    if not required.issubset(_table_columns(connection, "governance_assets")):
        return
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO archived_test_dataset_assets (
            legacy_asset_id, agent_id, title, body, source_improvement_id, inherited_from,
            created_at, updated_at, archived_at, reason
        )
        SELECT asset_id, agent_id, title, body, source_improvement_id, inherited_from,
               created_at, updated_at, ?, 'replaced_by_typed_test_dataset'
        FROM governance_assets WHERE asset_type = 'test_dataset'
        """,
        (utc_now(),),
    )
    connection.exec_driver_sql("DELETE FROM governance_assets WHERE asset_type = 'test_dataset'")


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}
