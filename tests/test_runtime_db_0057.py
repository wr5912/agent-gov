from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import cast

import pytest
from app.runtime.recovery_cli_support import OperatorRecoveryError
from app.runtime.runtime_db import SchemaMigration, make_engine, make_session_factory
from app.runtime.workspace_activation_recovery import (
    RecoveryAttemptOutcomeInput,
    WorkspaceActivationRecoveryAttemptStore,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.workspace_activation_recovery_test_support import (
    _DIGEST,
    ForeignKeySignature,
    RecoveryHarness,
    RecoverySchemaSignature,
    build_recovery_harness,
    recovery_request,
    recovery_schema_signature,
)

_MIGRATION = "0057_workspace_activation_operator_recovery"
_TABLE = "agent_workspace_activation_recovery_attempts"
_COMPLETED_EVIDENCE: dict[str, object] = {
    "activation_state": "completed",
    "already_applied": False,
    "repaired_ref_names": [],
    "resolution": "completed",
}
_INVALID_COMPLETION_EVIDENCE: tuple[dict[str, object], ...] = (
    {},
    {"activation_state": "completed"},
    {"activation_state": "completed", "resolution": "rejected"},
    {"activation_state": "pending", "resolution": "pending"},
    {**_COMPLETED_EVIDENCE, "already_applied": 1},
    {**_COMPLETED_EVIDENCE, "repaired_ref_names": "candidate"},
    {**_COMPLETED_EVIDENCE, "repaired_ref_names": ["candidate", "candidate"]},
    {**_COMPLETED_EVIDENCE, "repaired_ref_names": ["unknown"]},
    {**_COMPLETED_EVIDENCE, "extra": "not-authoritative"},
)
_INVALID_RAW_TERMINAL_EVIDENCE: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
    ("completed", {}, {}),
    ("completed", {**_COMPLETED_EVIDENCE, "resolution": "rejected"}, {}),
    ("completed", {**_COMPLETED_EVIDENCE, "already_applied": 1}, {}),
    (
        "completed",
        {**_COMPLETED_EVIDENCE, "repaired_ref_names": ["candidate", "candidate"]},
        {},
    ),
    ("completed", {**_COMPLETED_EVIDENCE, "repaired_ref_names": ["unknown"]}, {}),
    ("completed", {**_COMPLETED_EVIDENCE, "extra": True}, {}),
    ("failed", {}, {}),
    ("failed", {}, {"code": ""}),
    ("failed", {}, {"code": "not_stable"}),
    ("failed", {}, {"code": "STABLE_CODE", "extra": True}),
    ("failed", _COMPLETED_EVIDENCE, {"code": "STABLE_CODE"}),
)


@pytest.fixture(name="harness")
def _harness_fixture(tmp_path: Path) -> RecoveryHarness:
    return build_recovery_harness(tmp_path)


def test_0057_fresh_and_0056_upgrade_twice_have_exact_schema_authority(
    tmp_path: Path,
) -> None:
    fresh_path = tmp_path / "fresh.sqlite3"
    make_session_factory(fresh_path)
    fresh_factory = make_session_factory(fresh_path)

    upgrade_path = tmp_path / "upgrade.sqlite3"
    upgrade_factory = make_session_factory(upgrade_path)
    with upgrade_factory.begin() as db:
        assert db.get(SchemaMigration, "0056_agent_deletion_operations") is not None
        marker = db.get(SchemaMigration, _MIGRATION)
        assert marker is not None
        db.delete(marker)
    with make_engine(upgrade_path).begin() as connection:
        connection.exec_driver_sql(f"DROP TABLE {_TABLE}")
    make_session_factory(upgrade_path)
    upgraded_factory = make_session_factory(upgrade_path)

    with make_engine(fresh_path).connect() as connection:
        fresh = recovery_schema_signature(connection)
    with make_engine(upgrade_path).connect() as connection:
        upgraded = recovery_schema_signature(connection)
    assert fresh == upgraded
    _assert_exact_schema(fresh)
    with fresh_factory() as db:
        assert db.get(SchemaMigration, _MIGRATION) is not None
    with upgraded_factory() as db:
        assert db.get(SchemaMigration, _MIGRATION) is not None


def test_0057_enforces_fk_defaults_and_append_only_update_shapes(
    harness: RecoveryHarness,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)
    engine = harness.Session.kw["bind"]

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {_TABLE} SET result_json = '{{\"tampered\": true}}' WHERE recovery_id = ?",
            (request.recovery_id,),
        )
    started = store.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )
    assert started.state == "reserved"
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {_TABLE} SET observed_context_digest = ?, state = 'failed', "
            "error_json = '{\"code\":\"tampered\"}', completed_at = 'now' "
            "WHERE recovery_id = ?",
            ("sha256:" + "b" * 64, request.recovery_id),
        )
    completed = store.complete(
        request.recovery_id,
        outcome={"activation_state": "completed", "resolution": "completed"},
    )
    assert completed.state == "completed"
    for statement in (
        f"UPDATE {_TABLE} SET result_json = '{{\"tampered\": true}}' WHERE recovery_id = ?",
        f"UPDATE {_TABLE} SET operator = 'changed' WHERE recovery_id = ?",
        f"DELETE FROM {_TABLE} WHERE recovery_id = ?",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql(statement, (request.recovery_id,))

    foreign_key_path = harness.data_dir / "foreign-key.sqlite3"
    make_session_factory(foreign_key_path)
    with pytest.raises(IntegrityError), make_engine(foreign_key_path).begin() as connection:
        connection.exec_driver_sql(
            f"INSERT INTO {_TABLE} (recovery_id, operation_id, agent_id, action, state, "
            "requested_state_digest, operator, reason, created_at, updated_at) "
            "VALUES (?, ?, 'agent-a', 'reconcile', 'reserved', ?, 'operator', 'reason', 'now', 'now')",
            (f"war-{uuid.uuid4()}", f"wao-{uuid.uuid4()}", _DIGEST),
        )


def test_0057_store_rejects_invalid_or_ambiguous_completion_evidence(
    harness: RecoveryHarness,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)
    store.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )

    for outcome in _INVALID_COMPLETION_EVIDENCE:
        with pytest.raises(OperatorRecoveryError) as exc_info:
            store.complete(
                request.recovery_id,
                outcome=cast(RecoveryAttemptOutcomeInput, outcome),
            )
        assert exc_info.value.code == "RECOVERY_ATTEMPT_EVIDENCE_INVALID"
    assert store.require_reserved(request.recovery_id).result == {}


def test_0057_store_persists_one_stable_failure_code(
    harness: RecoveryHarness,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)

    failed = store.fail(request.recovery_id, code="错误")

    assert failed.state == "failed"
    assert failed.result == {}
    assert failed.error == {"code": "RECOVERY_FAILED"}
    assert store.fail(request.recovery_id, code="RECOVERY_FAILED") == failed


def test_0057_raw_sql_rejects_invalid_terminal_evidence(
    harness: RecoveryHarness,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)
    store.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )
    engine = harness.Session.kw["bind"]

    for state, result, error in _INVALID_RAW_TERMINAL_EVIDENCE:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE {_TABLE} SET state = ?, result_json = ?, error_json = ?, updated_at = 'terminal', completed_at = 'terminal' WHERE recovery_id = ?",
                (state, json.dumps(result), json.dumps(error), request.recovery_id),
            )

    assert store.require_reserved(request.recovery_id).state == "reserved"


def test_0057_projection_and_resume_reject_corrupt_persisted_completion(
    harness: RecoveryHarness,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)
    store.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )
    engine = harness.Session.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER ck_workspace_activation_recovery_reserved_update_shape")
        connection.exec_driver_sql("DROP TRIGGER ck_workspace_activation_recovery_terminal_evidence")
        connection.exec_driver_sql(
            f"UPDATE {_TABLE} SET state = 'completed', result_json = '{{}}', updated_at = 'corrupt', completed_at = 'corrupt' WHERE recovery_id = ?",
            (request.recovery_id,),
        )

    with pytest.raises(OperatorRecoveryError) as projection_error:
        store.get(request.recovery_id)
    assert projection_error.value.code == "RECOVERY_ATTEMPT_EVIDENCE_INVALID"
    with pytest.raises(OperatorRecoveryError) as resume_error:
        harness.service.resume(request.recovery_id)
    assert resume_error.value.code == "RECOVERY_ATTEMPT_EVIDENCE_INVALID"


@pytest.mark.parametrize("insert_shape", ["completed", "started", "failed"])
def test_0057_rejects_non_pristine_first_insert(
    harness: RecoveryHarness,
    insert_shape: str,
) -> None:
    values = {
        "recovery_id": f"war-{uuid.uuid4()}",
        "operation_id": harness.operation_id,
        "state": "reserved",
        "observed_state_digest": None,
        "observed_context_digest": None,
        "result_json": "{}",
        "error_json": "{}",
        "started_at": None,
        "completed_at": None,
    }
    if insert_shape == "completed":
        values.update(
            state="completed",
            observed_state_digest=_DIGEST,
            observed_context_digest=_DIGEST,
            result_json=json.dumps({"resolution": "completed"}),
            started_at="now",
            completed_at="now",
        )
    elif insert_shape == "started":
        values.update(
            observed_state_digest=_DIGEST,
            observed_context_digest=_DIGEST,
            started_at="now",
        )
    else:
        values.update(
            state="failed",
            error_json=json.dumps({"code": "DIRECT_INSERT"}),
            completed_at="now",
        )
    engine = harness.Session.kw["bind"]
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            INSERT INTO {_TABLE} (
                recovery_id, operation_id, agent_id, action, state,
                requested_state_digest, observed_state_digest,
                observed_context_digest, operator, reason, result_json,
                error_json, created_at, started_at, updated_at, completed_at
            ) VALUES (
                :recovery_id, :operation_id, 'agent-a', 'reconcile', :state,
                '{_DIGEST}', :observed_state_digest, :observed_context_digest,
                'operator', 'reason', :result_json, :error_json,
                'now', :started_at, 'now', :completed_at
            )
            """,
            values,
        )


def test_0057_reserve_commit_ack_loss_returns_exact_durable_attempt(
    harness: RecoveryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    _raise_after_next_successful_commit(monkeypatch)

    reserved = store.reserve(request)

    assert reserved.state == "reserved"
    assert store.reserve(request) == reserved


def test_0057_mark_started_commit_ack_loss_returns_exact_observation(
    harness: RecoveryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)
    _raise_after_next_successful_commit(monkeypatch)

    started = store.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )

    assert started.observed_state_digest == request.state_digest
    assert started.observed_context_digest == _DIGEST


def test_0057_finish_commit_ack_loss_returns_exact_terminal_evidence(
    harness: RecoveryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)
    store.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )
    outcome = {"activation_state": "completed", "resolution": "completed"}
    _raise_after_next_successful_commit(monkeypatch)

    completed = store.complete(request.recovery_id, outcome=outcome)

    assert completed.state == "completed"
    assert completed.result == {
        "activation_state": "completed",
        "already_applied": False,
        "repaired_ref_names": [],
        "resolution": "completed",
    }
    assert store.complete(request.recovery_id, outcome=outcome) == completed


def test_0057_finish_ack_loss_does_not_accept_different_terminal_evidence(
    harness: RecoveryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = recovery_request(harness)
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    store.reserve(request)
    store.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )
    engine = harness.Session.kw["bind"]
    original_commit = Session.commit

    def commit_different_outcome(db: Session) -> None:
        db.rollback()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE {_TABLE} SET state = 'completed', result_json = ?, updated_at = 'other', completed_at = 'other' WHERE recovery_id = ?",
                (
                    json.dumps(
                        {
                            "activation_state": "rejected",
                            "already_applied": False,
                            "repaired_ref_names": [],
                            "resolution": "rejected",
                        }
                    ),
                    request.recovery_id,
                ),
            )
        raise RuntimeError("simulated commit acknowledgement loss")

    monkeypatch.setattr(Session, "commit", commit_different_outcome)
    with pytest.raises(OperatorRecoveryError) as exc_info:
        store.complete(
            request.recovery_id,
            outcome={"activation_state": "completed", "resolution": "completed"},
        )
    assert getattr(exc_info.value, "code", None) == "RECOVERY_ATTEMPT_PERSISTENCE_FAILED"
    monkeypatch.setattr(Session, "commit", original_commit)
    persisted = store.get(request.recovery_id)
    assert persisted is not None
    assert persisted.result == {
        "activation_state": "rejected",
        "already_applied": False,
        "repaired_ref_names": [],
        "resolution": "rejected",
    }


def _raise_after_next_successful_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    original_commit = Session.commit
    armed = True

    def commit_with_lost_ack(db: Session) -> None:
        nonlocal armed
        original_commit(db)
        if armed:
            armed = False
            raise RuntimeError("simulated commit acknowledgement loss")

    monkeypatch.setattr(Session, "commit", commit_with_lost_ack)


def _assert_exact_schema(schema: RecoverySchemaSignature) -> None:
    columns = {
        column.name: (
            column.type,
            column.not_null,
            column.default,
            column.primary_key,
        )
        for column in schema.columns
    }
    assert columns == {
        "recovery_id": ("VARCHAR(128)", 1, None, 1),
        "operation_id": ("VARCHAR(128)", 1, None, 0),
        "agent_id": ("VARCHAR(128)", 1, None, 0),
        "action": ("VARCHAR(32)", 1, None, 0),
        "state": ("VARCHAR(32)", 1, None, 0),
        "requested_state_digest": ("VARCHAR(80)", 1, None, 0),
        "observed_state_digest": ("VARCHAR(80)", 0, None, 0),
        "observed_context_digest": ("VARCHAR(80)", 0, None, 0),
        "operator": ("VARCHAR(128)", 1, None, 0),
        "reason": ("TEXT", 1, None, 0),
        "result_json": ("JSON", 1, "'{}'", 0),
        "error_json": ("JSON", 1, "'{}'", 0),
        "created_at": ("VARCHAR(64)", 1, None, 0),
        "started_at": ("VARCHAR(64)", 0, None, 0),
        "updated_at": ("VARCHAR(64)", 1, None, 0),
        "completed_at": ("VARCHAR(64)", 0, None, 0),
    }
    explicit_indexes = {item.name: item for item in schema.indexes if item.origin == "c"}
    assert set(explicit_indexes) == {
        "ix_agent_workspace_activation_recovery_attempts_action",
        "ix_agent_workspace_activation_recovery_attempts_agent_id",
        "ix_agent_workspace_activation_recovery_attempts_created_at",
        "ix_agent_workspace_activation_recovery_attempts_operation_id",
        "ix_agent_workspace_activation_recovery_attempts_state",
        "ix_agent_workspace_activation_recovery_attempts_updated_at",
        "ix_workspace_activation_recovery_operation_created",
        "ux_workspace_activation_recovery_active_operation",
    }
    index_shapes = {name: (item.unique, item.partial, item.columns, item.predicate) for name, item in explicit_indexes.items()}
    assert index_shapes == {
        "ix_agent_workspace_activation_recovery_attempts_action": (0, 0, ("action",), ""),
        "ix_agent_workspace_activation_recovery_attempts_agent_id": (0, 0, ("agent_id",), ""),
        "ix_agent_workspace_activation_recovery_attempts_created_at": (0, 0, ("created_at",), ""),
        "ix_agent_workspace_activation_recovery_attempts_operation_id": (0, 0, ("operation_id",), ""),
        "ix_agent_workspace_activation_recovery_attempts_state": (0, 0, ("state",), ""),
        "ix_agent_workspace_activation_recovery_attempts_updated_at": (0, 0, ("updated_at",), ""),
        "ix_workspace_activation_recovery_operation_created": (
            0,
            0,
            ("operation_id", "created_at", "recovery_id"),
            "",
        ),
        "ux_workspace_activation_recovery_active_operation": (
            1,
            1,
            ("operation_id",),
            "state = 'reserved'",
        ),
    }
    _assert_triggers_and_foreign_keys(schema)


def _assert_triggers_and_foreign_keys(schema: RecoverySchemaSignature) -> None:
    assert {item.name for item in schema.triggers} == {
        "ck_workspace_activation_recovery_action_insert",
        "ck_workspace_activation_recovery_action_update",
        "ck_workspace_activation_recovery_identity_immutable",
        "ck_workspace_activation_recovery_insert_shape",
        "ck_workspace_activation_recovery_no_delete",
        "ck_workspace_activation_recovery_reserved_update_shape",
        "ck_workspace_activation_recovery_state_insert",
        "ck_workspace_activation_recovery_state_transition",
        "ck_workspace_activation_recovery_state_update",
        "ck_workspace_activation_recovery_terminal_evidence",
        "ck_workspace_activation_recovery_terminal_immutable",
    }
    assert schema.foreign_keys == (
        ForeignKeySignature(
            "agent_workspace_activation_operations",
            "operation_id",
            "operation_id",
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    )
