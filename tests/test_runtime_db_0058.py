from __future__ import annotations

import pytest
from app.runtime.runtime_db_migrations_0055 import (
    migrate_0055_workspace_activation_operations,
)
from app.runtime.runtime_db_migrations_0057 import (
    migrate_0057_workspace_activation_operator_recovery,
)
from app.runtime.runtime_db_migrations_0058 import (
    migrate_0058_workspace_activation_authority_hardening,
)
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError


def test_0058_reinstalls_activation_and_recovery_authority_idempotently() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        migrate_0055_workspace_activation_operations(connection)
        migrate_0057_workspace_activation_operator_recovery(connection)
        _remove_post_0057_authority(connection)
        _insert_activation(connection, "wao-before")
        connection.exec_driver_sql("UPDATE agent_workspace_activation_operations SET state = 'rejected' WHERE operation_id = 'wao-before'")
        connection.exec_driver_sql("DELETE FROM agent_workspace_activation_operations WHERE operation_id = 'wao-before'")

        migrate_0058_workspace_activation_authority_hardening(connection)
        migrate_0058_workspace_activation_authority_hardening(connection)
        _insert_activation(connection, "wao-after")

        with pytest.raises(IntegrityError, match="terminal workspace activation is immutable"):
            connection.exec_driver_sql("UPDATE agent_workspace_activation_operations SET state = 'rejected' WHERE operation_id = 'wao-after'")
        with pytest.raises(IntegrityError, match="cannot be deleted"):
            connection.exec_driver_sql("DELETE FROM agent_workspace_activation_operations WHERE operation_id = 'wao-after'")

        _insert_recovery_attempt(connection, "war-after", "wao-after")
        with pytest.raises(IntegrityError, match="terminal evidence"):
            connection.exec_driver_sql(
                """
                UPDATE agent_workspace_activation_recovery_attempts
                SET state = 'completed', completed_at = 'terminal',
                    updated_at = 'terminal'
                WHERE recovery_id = 'war-after'
                """
            )


def _remove_post_0057_authority(connection) -> None:
    for trigger in (
        "ck_workspace_activation_state_transition_update",
        "ck_workspace_activation_terminal_immutable_update",
        "ck_workspace_activation_no_delete",
        "ck_workspace_activation_graph_immutable_update",
        "ck_workspace_activation_recovery_terminal_evidence",
    ):
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")


def _insert_activation(connection, operation_id: str) -> None:
    connection.exec_driver_sql(
        """
        INSERT INTO agent_workspace_activation_operations (
            operation_id, agent_id, action, state, original_head_sha,
            original_index_fingerprint, original_workspace_fingerprint,
            maintenance_token, maintenance_generation, maintenance_expires_at,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, 'import_overwrite', 'completed', ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            operation_id,
            f"agent-{operation_id}",
            "a" * 40,
            "b" * 64,
            "c" * 64,
            f"token-{operation_id}",
            "2099-01-01T00:00:00+00:00",
            "2026-08-10T00:00:00+00:00",
            "2026-08-10T00:00:00+00:00",
            "2026-08-10T00:00:00+00:00",
        ),
    )


def _insert_recovery_attempt(
    connection,
    recovery_id: str,
    operation_id: str,
) -> None:
    digest = "sha256:" + "d" * 64
    connection.exec_driver_sql(
        """
        INSERT INTO agent_workspace_activation_recovery_attempts (
            recovery_id, operation_id, agent_id, action, state,
            requested_state_digest, observed_state_digest,
            observed_context_digest, operator, reason, result_json,
            error_json, created_at, started_at, updated_at, completed_at
        ) VALUES (
            ?, ?, ?, 'reconcile', 'reserved', ?, NULL, NULL, 'operator', 'reason',
            '{}', '{}', 'now', NULL, 'now', NULL
        )
        """,
        (
            recovery_id,
            operation_id,
            f"agent-{operation_id}",
            digest,
        ),
    )
    connection.exec_driver_sql(
        """
        UPDATE agent_workspace_activation_recovery_attempts
        SET observed_state_digest = ?, observed_context_digest = ?,
            started_at = 'started', updated_at = 'started'
        WHERE recovery_id = ?
        """,
        (digest, digest, recovery_id),
    )
