from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import app.services.agent_workspace_activation_reconciliation as activation_reconciliation
import pytest
from app.runtime import runtime_db as runtime_db_module
from app.runtime import service_launcher
from app.runtime.agent_admission import AgentMaintenanceClaim
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import (
    AgentAdmissionStateModel,
    AgentWorkspaceActivationOperationModel,
)
from app.runtime.claude_user_input_db import ClaudeUserInputRequestModel
from app.runtime.recovery_cli_support import (
    OperatorRecoveryError,
    build_workspace_activation_recovery_parser,
    run_workspace_activation_recovery_cli,
)
from app.runtime.runtime_db_base import utc_now
from app.runtime.settings import AppSettings
from app.runtime.workspace_activation_recovery import (
    RecoveryAttemptRequest,
    WorkspaceActivationRecoveryAttemptStore,
)
from app.services.agent_workspace_activation import (
    WorkspaceActivationFailure,
    WorkspaceActivationService,
)
from app.services.agent_workspace_activation_contracts import WorkspaceActivationVerificationError
from app.services.agent_workspace_activation_recovery import (
    RecoveryOperatorContext,
    WorkspaceActivationOperatorRecoveryService,
    build_default_workspace_activation_recovery_service,
)
from app.services.agent_workspace_git_operations import (
    SnapshotState,
    TreeReplacement,
    WorkspaceObservation,
)
from tests.workspace_activation_recovery_test_support import (
    _DIGEST,
    CONCURRENCY_TIMEOUT_SECONDS,
    HostileGitConfiguration,
    add_accepted_import_audit,
    build_recovery_harness,
    exact_operator_service,
    inspect_missing_workspace,
    install_hostile_git_configuration,
    recovery_apply_cli_args,
    remove_activation_refs,
    run_apply_resume_race,
    stage_preparing_recovery,
    stage_restore_outcome,
)
from tests.workspace_activation_recovery_test_support import (
    RecoveryHarness as _Harness,
)
from tests.workspace_activation_recovery_test_support import (
    activation_authority as _activation_authority,
)
from tests.workspace_activation_recovery_test_support import (
    activation_service as _activation_service,
)
from tests.workspace_activation_recovery_test_support import (
    convert_to_restore_recovery as _convert_to_restore_recovery,
)
from tests.workspace_activation_recovery_test_support import (
    git as _git,
)
from tests.workspace_activation_recovery_test_support import (
    recovery_request as _request,
)
from tests.workspace_activation_terminal_test_support import stage_completed_terminal_for_resume


@pytest.fixture(name="harness")
def _harness_fixture(tmp_path: Path) -> _Harness:
    return build_recovery_harness(tmp_path)


def test_read_only_inspection_is_redacted_and_never_constructs_git_store(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_constructor(*_args, **_kwargs):
        raise AssertionError("read-only inspection constructed GitAgentVersionStore")

    monkeypatch.setattr(GitAgentVersionStore, "__init__", forbidden_constructor)
    result = harness.service.inspect(harness.operation_id)
    encoded = json.dumps(result, ensure_ascii=False)
    assert result["available_actions"] == ["reconcile"]
    assert result["repository"]["head_position"] == "candidate"
    for secret in (
        "maintenance-super-secret",
        "secret-status-entry",
        "suite-secret",
        "diagnostic-secret",
        "error-secret",
        str(harness.workspace),
    ):
        assert secret not in encoded


def test_read_only_inspection_blocks_hostile_git_execution_and_lazy_fetch(
    harness: _Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.workspace.joinpath("nested-agent", "nested.txt").write_text(
        "nested-dirty\n",
        encoding="utf-8",
    )
    baseline = harness.service.inspector.inspect(harness.operation_id)
    hostile: HostileGitConfiguration = install_hostile_git_configuration(
        harness,
        tmp_path,
        monkeypatch,
    )

    observed = harness.service.inspector.inspect(harness.operation_id)

    assert observed.state_digest != baseline.state_digest
    assert observed.repository.available is False
    assert observed.repository.error_code == "WORKSPACE_REPOSITORY_INSPECTION_FAILED"
    assert not any(marker.exists() for marker in hostile.markers)
    failed_once = harness.service.inspector.inspect(harness.operation_id)
    failed_twice = harness.service.inspector.inspect(harness.operation_id)
    encoded = json.dumps(failed_once.to_payload(), ensure_ascii=False)
    assert failed_once.state_digest == failed_twice.state_digest
    assert failed_once.repository.error_code == "WORKSPACE_REPOSITORY_INSPECTION_FAILED"
    assert not any(marker.exists() for marker in hostile.markers)
    assert hostile.private_value not in encoded
    assert str(hostile.private_path) not in encoded


def test_inspection_digest_changes_with_durable_ref_state(harness: _Harness) -> None:
    before = harness.service.inspector.inspect(harness.operation_id)
    _git(
        harness.workspace,
        "update-ref",
        "-d",
        f"refs/agentgov/workspace-activations/{harness.operation_id}/candidate",
    )
    after = harness.service.inspector.inspect(harness.operation_id)
    assert before.state_digest != after.state_digest
    assert after.repair_available is True
    assert after.missing_refs == ("candidate",)


def test_apply_rejects_stale_digest_and_persists_failed_attempt(harness: _Harness) -> None:
    request = _request(harness, digest=_DIGEST)
    with pytest.raises(OperatorRecoveryError, match="state changed") as exc_info:
        harness.service.apply(request)
    assert exc_info.value.code == "STATE_DIGEST_MISMATCH"
    attempt = WorkspaceActivationRecoveryAttemptStore(harness.Session).get(request.recovery_id)
    assert attempt is not None
    assert attempt.state == "failed"
    assert attempt.error == {"code": "STATE_DIGEST_MISMATCH"}
    assert not harness.exact_calls


def test_exact_reconcile_rejects_observed_admission_mismatch(harness: _Harness) -> None:
    with harness.Session.begin() as db:
        admission = db.get(AgentAdmissionStateModel, "agent-a")
        assert admission is not None
        admission.maintenance_token = "different-maintenance-owner"
        admission.updated_at = utc_now()
    request = _request(harness)

    with pytest.raises(OperatorRecoveryError) as exc_info:
        harness.service.apply(request)

    attempt = WorkspaceActivationRecoveryAttemptStore(harness.Session).get(request.recovery_id)
    assert exc_info.value.code == "ACTIVATION_EXACT_EVIDENCE_MISMATCH"
    assert attempt is not None and attempt.state == "failed"
    assert not harness.exact_calls


def test_preflight_tracks_state_aware_audit_contract(harness: _Harness) -> None:
    pre_outcome = harness.service.inspector.inspect(harness.operation_id)
    assert pre_outcome.audit_status == "absent"
    assert pre_outcome.audit_consistent is True

    with harness.Session.begin() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, harness.operation_id)
        assert operation is not None
        operation.state = "completing"
        operation.recovery_phase = "completion_outcome"
    completing = harness.service.inspector.inspect(harness.operation_id)
    assert completing.audit_consistent is False
    assert "audit_conflict" in completing.reconcile_blockers

    add_accepted_import_audit(harness)
    with harness.Session.begin() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, harness.operation_id)
        assert operation is not None
        operation.state = "rejecting"
        operation.recovery_phase = "rejection_outcome"
        operation.error_json = {"error_code": "REJECTED", "detail": "exact failure"}
    rejecting = harness.service.inspector.inspect(harness.operation_id)
    assert rejecting.audit_status == "accepted"
    assert rejecting.audit_consistent is False
    assert rejecting.to_payload()["available_actions"] == []


def test_preflight_includes_admission_generation_and_waiting_hitl(
    harness: _Harness,
) -> None:
    before = harness.service.inspector.inspect(harness.operation_id)
    with harness.Session.begin() as db:
        admission = db.get(AgentAdmissionStateModel, "agent-a")
        assert admission is not None
        admission.generation += 1
    generation_mismatch = harness.service.inspector.inspect(harness.operation_id)
    assert generation_mismatch.state_digest != before.state_digest
    assert "admission_claim_mismatch" in generation_mismatch.reconcile_blockers

    with harness.Session.begin() as db:
        admission = db.get(AgentAdmissionStateModel, "agent-a")
        assert admission is not None
        admission.generation -= 1
        db.add(
            runtime_db_module.SessionRecordModel(
                session_id="stale-session",
                agent_id="agent-a",
                active_run_id="stale-run",
                active_run_expires_at="2000-01-01T00:00:00+00:00",
            )
        )
        db.add(
            ClaudeUserInputRequestModel(
                request_id="waiting-hitl",
                decision_token_hash="digest",
                business_agent_id="agent-a",
                run_id="waiting-run",
                api_session_id="waiting-session",
                request_type="permission",
                tool_name="Bash",
                status="waiting",
                expires_at="2099-01-01T00:00:00+00:00",
            )
        )
    blocked = harness.service.inspector.inspect(harness.operation_id)
    assert blocked.active_session_count == 0
    assert blocked.active_hitl_count == 1
    assert "active_runtime_work" in blocked.reconcile_blockers
    assert blocked.to_payload()["available_actions"] == []


def test_repair_missing_refs_is_strict_cas_then_exact_reconcile(harness: _Harness) -> None:
    _git(
        harness.workspace,
        "update-ref",
        "-d",
        f"refs/agentgov/workspace-activations/{harness.operation_id}/candidate",
    )
    request = _request(harness, action="repair_missing_refs")
    result = harness.service.apply(request)
    assert result["state"] == "completed"
    assert result["result"]["repaired_ref_names"] == ["candidate"]
    assert not _git(
        harness.workspace,
        "for-each-ref",
        f"refs/agentgov/workspace-activations/{harness.operation_id}",
    )
    assert harness.exact_calls[0].recovery_id == request.recovery_id
    assert harness.exact_calls[0].expected_state_digest == request.state_digest
    assert harness.service.apply(request) == result


def test_preparing_exact_empty_reconciles_from_original_evidence(
    harness: _Harness,
) -> None:
    stage_preparing_recovery(harness)
    operator = exact_operator_service(harness)
    inspection = operator.inspector.inspect(harness.operation_id)
    request = _request(harness, digest=inspection.state_digest)

    result = operator.apply(request)

    assert inspection.to_payload()["available_actions"] == ["reconcile"]
    assert result["state"] == "completed"
    assert result["result"]["resolution"] == "rejected"


def test_staged_empty_rejection_survives_unreachable_candidate_objects(
    harness: _Harness,
) -> None:
    stage_restore_outcome(harness, target="rejection_outcome")
    remove_activation_refs(harness)
    _git(harness.workspace, "reflog", "expire", "--expire=now", "--all")
    _git(harness.workspace, "gc", "--prune=now")
    operator = exact_operator_service(harness)
    inspection = operator.inspector.inspect(harness.operation_id)

    result = operator.apply(_request(harness, digest=inspection.state_digest))

    assert inspection.repository.available is True
    assert inspection.repository.refs == {}
    assert inspection.to_payload()["available_actions"] == ["reconcile"]
    assert result["result"]["resolution"] == "rejected"


def test_staged_empty_inspection_rejects_malformed_graph_shape(harness: _Harness) -> None:
    stage_restore_outcome(harness, target="completion_outcome")
    remove_activation_refs(harness)
    # The ORM-only harness intentionally models a historical row created before 0055 triggers.
    with harness.Session.begin() as db:
        connection = db.connection()
        connection.exec_driver_sql(
            "UPDATE agent_workspace_activation_operations SET snapshot_created = 1 WHERE operation_id = ?",
            (harness.operation_id,),
        )

    inspection = exact_operator_service(harness).inspector.inspect(harness.operation_id)

    assert inspection.repository.graph_valid is False
    assert "object_graph_invalid" in inspection.reconcile_blockers


def test_staged_partial_refs_require_operator_repair_before_exact_terminal(
    harness: _Harness,
) -> None:
    stage_restore_outcome(harness, target="completion_outcome")
    candidate_ref = f"refs/agentgov/workspace-activations/{harness.operation_id}/candidate"
    _git(harness.workspace, "update-ref", "-d", candidate_ref)
    periodic = _activation_service(harness)
    assert periodic.reconcile_operation(harness.operation_id) == "recovery_required"
    operator = exact_operator_service(harness)
    inspection = operator.inspector.inspect(harness.operation_id)
    request = RecoveryAttemptRequest(
        recovery_id=f"war-{uuid.uuid4()}",
        operation_id=harness.operation_id,
        action="repair_missing_refs",
        state_digest=inspection.state_digest,
        operator="replacement-operator",
        reason="repair exact staged durable refs",
    )

    result = operator.apply(request)

    assert inspection.to_payload()["available_actions"] == ["repair_missing_refs"]
    assert result["result"]["repaired_ref_names"] == ["candidate"]
    assert result["result"]["resolution"] == "completed"


def test_repair_ref_mismatch_and_dirty_workspace_fail_closed(harness: _Harness) -> None:
    prefix = f"refs/agentgov/workspace-activations/{harness.operation_id}"
    _git(harness.workspace, "update-ref", "-d", f"{prefix}/original-index-tree")
    original = _git(harness.workspace, "rev-parse", "HEAD^")
    _git(harness.workspace, "update-ref", f"{prefix}/candidate", original)
    mismatched = harness.service.inspector.inspect(harness.operation_id)
    assert "existing_ref_mismatch" in mismatched.repair_blockers
    assert mismatched.repair_available is False

    _git(harness.workspace, "update-ref", f"{prefix}/candidate", _git(harness.workspace, "rev-parse", "HEAD"))
    harness.workspace.joinpath("operator-note.txt").write_text("changed\n", encoding="utf-8")
    dirty = harness.service.inspector.inspect(harness.operation_id)
    assert "live_workspace_not_exact" in dirty.repair_blockers
    assert dirty.repair_available is False


def test_single_active_attempt_and_exact_identity_are_enforced(harness: _Harness) -> None:
    store = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    first = _request(harness)
    store.reserve(first)
    with pytest.raises(OperatorRecoveryError) as active_error:
        store.reserve(_request(harness))
    assert active_error.value.code == "RECOVERY_ATTEMPT_ACTIVE"
    conflict = RecoveryAttemptRequest(
        recovery_id=first.recovery_id,
        operation_id=first.operation_id,
        action=first.action,
        state_digest=first.state_digest,
        operator="different-operator",
        reason=first.reason,
    )
    with pytest.raises(OperatorRecoveryError) as identity_error:
        store.reserve(conflict)
    assert identity_error.value.code == "RECOVERY_ID_CONFLICT"


def test_reserved_attempt_excludes_periodic_and_rejects_live_state_writers(harness: _Harness) -> None:
    activation = _activation_service(harness)
    with harness.Session.begin() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, harness.operation_id)
        assert operation is not None
        operation.state = "prepared"
        operation.updated_at = utc_now()
    request = _request(harness)
    attempts = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    attempts.reserve(request)
    before = _activation_authority(harness)

    assert activation._reconciliation_candidates(limit=100) == []
    with pytest.raises(WorkspaceActivationVerificationError, match="Operator recovery owns"):
        activation.activate(harness.operation_id, before_activate=lambda: pytest.fail("activation callback ran"))
    with pytest.raises(WorkspaceActivationVerificationError, match="Operator recovery owns"):
        activation.reject(
            harness.operation_id,
            failure=WorkspaceActivationFailure("INJECTED", "must retain operator authority"),
        )
    operation = activation._require_operation(harness.operation_id)
    snapshot = SnapshotState(
        original_head=operation.original_head_sha,
        current_head=str(operation.base_commit_sha),
        snapshot_created=operation.snapshot_created,
        original_status=operation.original_status_text,
        original_index_tree_sha=operation.original_index_tree_sha,
        original_index_fingerprint=operation.original_index_fingerprint,
        original_index_snapshot=operation.original_index_snapshot,
        original_workspace_fingerprint=operation.original_workspace_fingerprint,
    )
    replacement = TreeReplacement(
        action="restored",
        previous_commit_sha=str(operation.base_commit_sha),
        current_commit_sha=str(operation.candidate_commit_sha),
        candidate_tree_sha=str(operation.candidate_tree_sha),
    )
    with pytest.raises(WorkspaceActivationVerificationError, match="Operator recovery owns"):
        activation.prepare_restore(
            harness.operation_id,
            snapshot=snapshot,
            replacement=replacement,
            target_commit_sha=str(operation.candidate_commit_sha),
        )
    claim = AgentMaintenanceClaim(
        agent_id=operation.agent_id,
        token=operation.maintenance_token,
        generation=operation.maintenance_generation,
        kind="workspace_import",
        owner_id=operation.operation_id,
        expires_at=operation.maintenance_expires_at,
    )
    observation = WorkspaceObservation(
        original_head=operation.original_head_sha,
        original_status=operation.original_status_text,
        original_index_fingerprint=operation.original_index_fingerprint,
        original_index_snapshot=operation.original_index_snapshot,
        original_workspace_fingerprint=operation.original_workspace_fingerprint,
    )
    with pytest.raises(WorkspaceActivationVerificationError, match="Operator recovery owns"):
        activation.begin_import(
            agent_id=operation.agent_id,
            observation=observation,
            claim=claim,
            package_sha256=str(operation.package_sha256),
            tree_sha256=str(operation.tree_sha256),
        )

    assert _activation_authority(harness) == before
    attempts.fail(request.recovery_id, code="TEST_RELEASE")


def test_fresh_recovery_required_exact_apply_bypasses_only_periodic_delay(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _convert_to_restore_recovery(harness)
    activation = _activation_service(harness)

    def periodic_delay_was_consulted(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        pytest.fail("typed exact recovery consulted periodic delay")

    monkeypatch.setattr(
        activation_reconciliation,
        "workspace_activation_is_reconcilable",
        periodic_delay_was_consulted,
    )

    def reconcile_exact(context: RecoveryOperatorContext) -> str:
        return activation.reconcile_exact_operation(
            context.operation_id,
            recovery_attempt_id=context.recovery_id,
            expected_state_digest=context.expected_state_digest,
        )

    operator = WorkspaceActivationOperatorRecoveryService(
        session_factory=harness.Session,
        data_dir=harness.data_dir,
        store_for=lambda _agent_id: harness.store,
        reconcile_exact=reconcile_exact,
    )
    request = _request(harness)

    result = operator.apply(request)

    assert result["state"] == "completed"
    assert activation._require_operation(harness.operation_id).state == "completed"


def test_exact_recovery_wins_stale_periodic_race_without_deadlock(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _convert_to_restore_recovery(harness)
    exact_activation = _activation_service(harness)
    periodic_activation = _activation_service(harness)
    periodic_prechecked = Event()
    release_periodic = Event()
    exact_entered = Event()
    release_exact = Event()
    original_authorized = periodic_activation._reconciliation_is_authorized

    def pause_after_periodic_precheck(operation_id, authority):  # type: ignore[no-untyped-def]
        allowed = original_authorized(operation_id, authority)
        if authority is None and allowed and not periodic_prechecked.is_set():
            periodic_prechecked.set()
            assert release_periodic.wait(CONCURRENCY_TIMEOUT_SECONDS)
        return allowed

    monkeypatch.setattr(periodic_activation, "_reconciliation_is_authorized", pause_after_periodic_precheck)

    def reconcile_exact(context: RecoveryOperatorContext) -> str:
        exact_entered.set()
        assert release_exact.wait(CONCURRENCY_TIMEOUT_SECONDS)
        return exact_activation.reconcile_exact_operation(
            context.operation_id,
            recovery_attempt_id=context.recovery_id,
            expected_state_digest=context.expected_state_digest,
        )

    operator = WorkspaceActivationOperatorRecoveryService(
        session_factory=harness.Session,
        data_dir=harness.data_dir,
        store_for=lambda _agent_id: harness.store,
        reconcile_exact=reconcile_exact,
    )
    request = _request(harness)
    before = _activation_authority(harness)
    with ThreadPoolExecutor(max_workers=2) as executor:
        periodic_future = executor.submit(periodic_activation.reconcile_operation, harness.operation_id)
        try:
            assert periodic_prechecked.wait(CONCURRENCY_TIMEOUT_SECONDS)
            operator_future = executor.submit(operator.apply, request)
            assert exact_entered.wait(CONCURRENCY_TIMEOUT_SECONDS)
            assert periodic_activation._reconciliation_candidates(limit=100) == []
            release_periodic.set()
            with pytest.raises(TimeoutError):
                periodic_future.result(timeout=0.1)
            release_exact.set()
            operator_result = operator_future.result(timeout=CONCURRENCY_TIMEOUT_SECONDS)
            periodic_result = periodic_future.result(timeout=CONCURRENCY_TIMEOUT_SECONDS)
        finally:
            release_periodic.set()
            release_exact.set()

    attempt = WorkspaceActivationRecoveryAttemptStore(harness.Session).get(request.recovery_id)
    assert before[-1] == "recovery_required"
    assert operator_result["state"] == "completed"
    assert periodic_result == "completed"
    assert attempt is not None and attempt.state == "completed"
    assert exact_activation._require_operation(harness.operation_id).state == "completed"
    assert operator.apply(request) == operator_result


def test_periodic_writer_finishes_before_operator_can_reserve(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _convert_to_restore_recovery(harness)
    periodic = _activation_service(harness)
    writer_holds_lock = Event()
    release_writer = Event()
    operator_seeks_lock = Event()
    authorization_checks = 0
    original_authorized = periodic._reconciliation_is_authorized

    def pause_writer_inside_lock(operation_id, authority):  # type: ignore[no-untyped-def]
        nonlocal authorization_checks
        allowed = original_authorized(operation_id, authority)
        if authority is None:
            authorization_checks += 1
            if authorization_checks == 2:
                writer_holds_lock.set()
                assert release_writer.wait(CONCURRENCY_TIMEOUT_SECONDS)
        return allowed

    monkeypatch.setattr(periodic, "_reconciliation_is_authorized", pause_writer_inside_lock)

    def store_for(_agent_id: str) -> GitAgentVersionStore:
        operator_seeks_lock.set()
        return harness.store

    def forbidden_exact(_context: RecoveryOperatorContext) -> str:
        pytest.fail("operator exact reconcile ran after the periodic writer won")

    operator = WorkspaceActivationOperatorRecoveryService(
        session_factory=harness.Session,
        data_dir=harness.data_dir,
        store_for=store_for,
        reconcile_exact=forbidden_exact,
    )
    request = _request(harness)
    attempts = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(periodic.reconcile_operation, harness.operation_id)
        try:
            assert writer_holds_lock.wait(CONCURRENCY_TIMEOUT_SECONDS)
            operator_future = executor.submit(operator.apply, request)
            assert operator_seeks_lock.wait(CONCURRENCY_TIMEOUT_SECONDS)
            assert attempts.get(request.recovery_id) is None
            release_writer.set()
            assert writer.result(timeout=CONCURRENCY_TIMEOUT_SECONDS) == "completed"
            with pytest.raises(OperatorRecoveryError) as exc_info:
                operator_future.result(timeout=CONCURRENCY_TIMEOUT_SECONDS)
        finally:
            release_writer.set()

    assert exc_info.value.code == "ACTIVATION_OPERATION_TERMINAL"
    assert attempts.get(request.recovery_id) is None
    assert periodic._require_operation(harness.operation_id).state == "completed"


def test_missing_repository_layout_fails_with_durable_attempt(
    harness: _Harness,
) -> None:
    harness.workspace.rename(harness.workspace.with_name("workspace-missing"))
    request = _request(harness)

    with pytest.raises(OperatorRecoveryError) as exc_info:
        harness.service.apply(request)

    attempt = WorkspaceActivationRecoveryAttemptStore(harness.Session).get(
        request.recovery_id,
    )
    assert exc_info.value.code == "WORKSPACE_REPOSITORY_UNAVAILABLE"
    assert attempt is not None and attempt.state == "failed"
    assert attempt.error == {"code": "WORKSPACE_REPOSITORY_UNAVAILABLE"}


def test_default_factory_uses_typed_exact_hook_without_creating_agent_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def reconcile_exact(
        _service: WorkspaceActivationService,
        operation_id: str,
        *,
        recovery_attempt_id: str,
        expected_state_digest: str,
    ) -> str:
        calls.append((operation_id, recovery_attempt_id, expected_state_digest))
        return "completed"

    monkeypatch.setattr(WorkspaceActivationService, "reconcile_exact_operation", reconcile_exact)
    settings = AppSettings(_env_file=None, DATA_DIR=tmp_path / "data")
    service = build_default_workspace_activation_recovery_service(settings)
    assert service._reconcile_exact is not None
    context = RecoveryOperatorContext(
        operation_id=f"wao-{uuid.uuid4()}",
        recovery_id=f"war-{uuid.uuid4()}",
        expected_state_digest=_DIGEST,
        operator="operator-a",
        reason="factory exact hook contract",
    )

    assert service._reconcile_exact(context) == "completed"
    assert calls == [(context.operation_id, context.recovery_id, context.expected_state_digest)]
    assert service._store_for is not None
    service._store_for("missing-agent")
    assert not (settings.data_dir / "business-agents" / "missing-agent").exists()


def test_cli_list_inspect_apply_and_no_force_surface(
    harness: _Harness,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_workspace_activation_recovery_parser()
    list_code = run_workspace_activation_recovery_cli(
        ["list"],
        service=harness.service,
    )
    listing = json.loads(capsys.readouterr().out)
    assert list_code == 0
    assert listing["mode"] == "read-only"
    code = run_workspace_activation_recovery_cli(
        ["inspect", "--operation-id", harness.operation_id],
        service=harness.service,
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["mode"] == "read-only"
    assert output["operation_id"] == harness.operation_id
    assert output["recovery_id"].startswith("war-")
    request = _request(harness, digest=output["state_digest"])
    apply_args = recovery_apply_cli_args(request)
    with pytest.raises(SystemExit):
        parser.parse_args([*apply_args, "--force"])
    apply_code = run_workspace_activation_recovery_cli(
        apply_args,
        service=harness.service,
    )
    applied = json.loads(capsys.readouterr().out)
    assert apply_code == 0
    assert applied["mode"] == "apply"
    assert applied["state"] == "completed"
    assert run_workspace_activation_recovery_cli(apply_args, service=harness.service) == 0
    assert json.loads(capsys.readouterr().out) == applied


def test_cli_resume_uses_reserved_journal_without_original_audit_text(
    harness: _Harness,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = _request(harness)
    WorkspaceActivationRecoveryAttemptStore(harness.Session).reserve(request)
    inspected = harness.service.inspect(harness.operation_id)
    listed = harness.service.list()
    active = inspected["active_recovery_attempt"]
    assert isinstance(active, dict)
    assert active["recovery_id"] == request.recovery_id
    assert active["requested_state_digest"] == request.state_digest
    assert listed[0]["active_recovery_attempt"]["action"] == "reconcile"
    parser = build_workspace_activation_recovery_parser()
    for forbidden in ("--action", "--operator", "--reason"):
        with pytest.raises(SystemExit):
            parser.parse_args(["resume", "--recovery-id", request.recovery_id, forbidden, "changed"])

    code = run_workspace_activation_recovery_cli(
        ["resume", "--recovery-id", request.recovery_id],
        service=harness.service,
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["mode"] == "resume"
    assert output["state"] == "completed"


def test_resume_closes_core_terminal_crash_and_rejects_wrong_attempts(
    harness: _Harness,
) -> None:
    request = _request(harness)
    attempts = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    attempts.reserve(request)
    attempts.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )
    stage_completed_terminal_for_resume(harness, request)
    resumed = harness.service.resume(request.recovery_id)
    assert resumed["state"] == "completed"
    assert resumed["result"]["already_applied"] is True
    with pytest.raises(OperatorRecoveryError) as nonreserved:
        harness.service.resume(request.recovery_id)
    assert nonreserved.value.code == "RECOVERY_ATTEMPT_NOT_RESERVED"
    with pytest.raises(OperatorRecoveryError) as missing:
        harness.service.resume(f"war-{uuid.uuid4()}")
    assert missing.value.code == "RECOVERY_ATTEMPT_NOT_FOUND"


def test_original_apply_and_resume_have_one_terminal_authority(
    harness: _Harness,
) -> None:
    outcome = run_apply_resume_race(harness)
    assert outcome.apply_state == "completed"
    assert outcome.resume_result in {"completed", "RECOVERY_ATTEMPT_NOT_RESERVED"}
    assert outcome.exact_call_count == 1
    assert outcome.attempt_state == "completed"


def test_read_only_inspection_does_not_create_missing_workspace(tmp_path: Path) -> None:
    result, data_dir = inspect_missing_workspace(tmp_path)
    assert result["repository"]["error_code"] == "WORKSPACE_REPOSITORY_UNAVAILABLE"
    assert not data_dir.exists()


def test_service_launcher_forwards_recovery_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    settings = AppSettings(_env_file=None, DATA_DIR=tmp_path / "data")
    monkeypatch.setattr(service_launcher, "get_settings", lambda: settings)
    monkeypatch.setattr(service_launcher, "default_runtime_bootstrap_dir", lambda: tmp_path)
    monkeypatch.setattr(service_launcher, "_selected_env", lambda _settings: {})
    monkeypatch.setattr(
        service_launcher,
        "_run_workspace_activation_recovery",
        lambda _settings, _bootstrap, _env, command: calls.append(list(command)) or 0,
    )
    recovery_id = f"war-{uuid.uuid4()}"
    assert service_launcher.main(["workspace-activation-recovery", "resume", "--recovery-id", recovery_id]) == 0
    assert calls == [["resume", "--recovery-id", recovery_id]]
