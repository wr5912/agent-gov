from __future__ import annotations

import json
import re

import pytest
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.runtime_db_migrations_0054 import migrate_0054_workspace_import_diagnostics
from app.runtime.runtime_db_migrations_0055 import migrate_0055_workspace_activation_operations
from app.runtime.state_machines import WORKSPACE_ACTIVATION_FENCE_STATES
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError


def _insert_activation(connection, operation_id: str, agent_id: str, *, state: str = "prepared") -> None:
    connection.exec_driver_sql(
        """
        INSERT INTO agent_workspace_activation_operations (
            operation_id, agent_id, action, state, original_head_sha,
            original_index_fingerprint, original_workspace_fingerprint,
            maintenance_token, maintenance_generation, maintenance_expires_at,
            created_at, updated_at
        ) VALUES (?, ?, 'import_overwrite', ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            operation_id,
            agent_id,
            state,
            "a" * 40,
            "b" * 64,
            "c" * 64,
            f"token-{operation_id}",
            "2099-01-01T00:00:00+00:00",
            "2026-08-09T00:00:00+00:00",
            "2026-08-09T00:00:00+00:00",
        ),
    )


def _predicate_states(sql: str) -> set[str]:
    match = re.search(r"(?:WHERE )?state IN \(([^)]*)\)", sql)
    assert match is not None
    return {value.strip().strip("'") for value in match.group(1).split(",")}


def test_0055_creates_workspace_activation_journal_idempotently() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE historical_runtime_row (row_id VARCHAR(32) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO historical_runtime_row VALUES ('preserved')")

        migrate_0055_workspace_activation_operations(connection)
        migrate_0055_workspace_activation_operations(connection)

        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_workspace_activation_operations)")}
        assert {
            "operation_id",
            "import_id",
            "agent_id",
            "action",
            "state",
            "original_head_sha",
            "base_commit_sha",
            "candidate_commit_sha",
            "candidate_tree_sha",
            "snapshot_created",
            "original_status_text",
            "original_index_fingerprint",
            "original_workspace_fingerprint",
            "original_index_tree_sha",
            "original_index_snapshot",
            "recovery_phase",
            "package_sha256",
            "tree_sha256",
            "suite_status",
            "suite_json",
            "diagnostics_json",
            "maintenance_generation",
            "maintenance_expires_at",
            "error_json",
            "created_at",
            "updated_at",
            "completed_at",
        } <= columns
        indexes = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA index_list(agent_workspace_activation_operations)")}
        assert "ux_agent_workspace_activation_operations_import" in indexes
        assert "ux_agent_workspace_activation_operations_fence" in indexes
        assert connection.exec_driver_sql("SELECT row_id FROM historical_runtime_row").scalar_one() == "preserved"


def test_0055_upgrades_0054_authority_and_adds_exact_provision_outcome_token() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_registry (
                agent_id VARCHAR(128) PRIMARY KEY,
                name VARCHAR(256) NOT NULL,
                category VARCHAR(32) NOT NULL,
                workspace_dir VARCHAR(2048) NOT NULL,
                created_at VARCHAR(64) NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_workspace_import_records (
                import_id VARCHAR(128) PRIMARY KEY,
                status VARCHAR(32) NOT NULL,
                suite_json JSON NOT NULL,
                warnings_json JSON NOT NULL
            )
            """
        )
        diagnostics = [{"level": "warning", "code": "W", "message": "preserve"}]
        connection.exec_driver_sql(
            "INSERT INTO agent_workspace_import_records VALUES ('awi-old', 'accepted', ?, ?)",
            (json.dumps({"diagnostics": diagnostics}), json.dumps(diagnostics)),
        )
        migrate_0054_workspace_import_diagnostics(connection)
        migrate_0055_workspace_activation_operations(connection)

        registry_columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}
        assert "provision_completed_token" in registry_columns
        row = connection.exec_driver_sql("SELECT suite_status, diagnostics_json FROM agent_workspace_import_records WHERE import_id = 'awi-old'").one()
        assert row.suite_status == "warning"
        assert json.loads(row.diagnostics_json) == diagnostics


def test_0055_enforces_state_action_phase_and_fence_predicate() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        migrate_0055_workspace_activation_operations(connection)
        create_sql = str(
            connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agent_workspace_activation_operations'").scalar_one()
        )
        assert "ck_workspace_activation_state" in create_sql
        assert "ck_workspace_activation_action" in create_sql
        assert "ck_workspace_activation_recovery_phase" in create_sql
        _insert_activation(connection, "op-valid", "agent-one")
        for column, invalid in (
            ("state", "unknown"),
            ("action", "unknown"),
            ("recovery_phase", "unknown"),
        ):
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    f"UPDATE agent_workspace_activation_operations SET {column} = ? WHERE operation_id = 'op-valid'",
                    (invalid,),
                )
        with pytest.raises(IntegrityError):
            _insert_activation(connection, "op-conflict", "agent-one", state="recovery_required")
        connection.exec_driver_sql("UPDATE agent_workspace_activation_operations SET state = 'completing' WHERE operation_id = 'op-valid'")
        connection.exec_driver_sql("UPDATE agent_workspace_activation_operations SET state = 'completed' WHERE operation_id = 'op-valid'")
        _insert_activation(connection, "op-next", "agent-one", state="recovery_required")
        fence_sql = str(
            connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ux_agent_workspace_activation_operations_fence'"
            ).scalar_one()
        )
        orm_index = next(
            index for index in AgentWorkspaceActivationOperationModel.__table__.indexes if index.name == "ux_agent_workspace_activation_operations_fence"
        )
        orm_predicate = str(orm_index.dialect_options["sqlite"]["where"])
        assert _predicate_states(fence_sql) == WORKSPACE_ACTIVATION_FENCE_STATES
        assert _predicate_states(orm_predicate) == WORKSPACE_ACTIVATION_FENCE_STATES


def test_0055_freezes_graph_identity_after_preparing_transition() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        migrate_0055_workspace_activation_operations(connection)
        _insert_activation(connection, "op-preparing", "agent-one", state="preparing")
        connection.exec_driver_sql(
            """
            UPDATE agent_workspace_activation_operations
            SET base_commit_sha = ?, candidate_commit_sha = ?, candidate_tree_sha = ?,
                original_index_tree_sha = ?, state = 'prepared'
            WHERE operation_id = 'op-preparing'
            """,
            ("a" * 40, "d" * 40, "e" * 40, "f" * 40),
        )
        for column, value in (
            ("original_head_sha", "0" * 40),
            ("candidate_tree_sha", "1" * 40),
            ("target_commit_sha", "2" * 40),
            ("snapshot_created", 1),
            ("original_status_text", "hostile status"),
            ("original_index_fingerprint", "3" * 64),
            ("original_workspace_fingerprint", "4" * 64),
            ("original_index_snapshot", b"hostile index"),
            ("package_sha256", "5" * 64),
            ("suite_json", '{"hostile":true}'),
            ("maintenance_token", "hostile-token"),
        ):
            with pytest.raises(IntegrityError, match="graph journal is immutable"):
                connection.exec_driver_sql(
                    f"UPDATE agent_workspace_activation_operations SET {column} = ? WHERE operation_id = 'op-preparing'",
                    (value,),
                )


@pytest.mark.parametrize("state", ["prepared", "completed", "rejected", "recovery_required"])
def test_0055_rejects_raw_sql_preparing_reentry(state: str) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        migrate_0055_workspace_activation_operations(connection)
        _insert_activation(connection, f"op-{state}", f"agent-{state}", state=state)
        with pytest.raises(
            IntegrityError,
            match="invalid workspace activation state transition|terminal workspace activation is immutable",
        ):
            connection.exec_driver_sql(
                "UPDATE agent_workspace_activation_operations SET state = 'preparing' WHERE operation_id = ?",
                (f"op-{state}",),
            )


def test_0055_enforces_state_transitions_terminal_immutability_and_no_delete() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        migrate_0055_workspace_activation_operations(connection)
        _insert_activation(connection, "op-lifecycle", "agent-lifecycle", state="prepared")
        with pytest.raises(IntegrityError, match="invalid workspace activation state transition"):
            connection.exec_driver_sql("UPDATE agent_workspace_activation_operations SET state = 'completed' WHERE operation_id = 'op-lifecycle'")
        for state in ("completing", "recovery_required", "rejecting", "rejected"):
            connection.exec_driver_sql(
                "UPDATE agent_workspace_activation_operations SET state = ? WHERE operation_id = 'op-lifecycle'",
                (state,),
            )
        with pytest.raises(IntegrityError, match="terminal workspace activation is immutable"):
            connection.exec_driver_sql(
                "UPDATE agent_workspace_activation_operations SET error_json = '{\"code\":\"changed\"}' WHERE operation_id = 'op-lifecycle'"
            )
        with pytest.raises(IntegrityError, match="cannot be deleted"):
            connection.exec_driver_sql("DELETE FROM agent_workspace_activation_operations WHERE operation_id = 'op-lifecycle'")
