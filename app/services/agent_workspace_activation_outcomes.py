from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.agent_testing.models import AgentTestRunModel, AgentWorkspaceImportRecordModel
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.claude_user_input_db import ClaudeUserInputRequestModel
from app.runtime.errors import SessionConflictError
from app.runtime.runtime_db import (
    AgentAdmissionStateModel,
    AgentWorkspaceActivationOperationModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    utc_now,
)
from app.services.agent_workspace_activation_audit import (
    validate_accepted_import_audit,
    validate_rejected_import_audit,
)
from app.services.agent_workspace_activation_contracts import WorkspaceActivationVerificationError
from app.services.agent_workspace_activation_refs import GitCommandError, verify_workspace_operation_refs
from app.services.agent_workspace_activation_verification import (
    verify_candidate,
    verify_rejected_workspace,
)

TerminalActivationOutcome = Literal["completed", "rejected"]


@dataclass(frozen=True)
class ExactTerminalActivation:
    operation_id: str
    outcome: TerminalActivationOutcome


def require_exact_admission_fence(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
) -> AgentAdmissionStateModel:
    admission = db.get(AgentAdmissionStateModel, operation.agent_id)
    expected_kind = _maintenance_kind(operation)
    expected = (
        operation.maintenance_token,
        operation.maintenance_generation,
        operation.maintenance_generation,
        expected_kind,
    )
    actual = (
        admission.maintenance_token if admission else None,
        admission.maintenance_generation if admission else None,
        admission.generation if admission else None,
        admission.maintenance_kind if admission else None,
    )
    if actual != expected:
        raise WorkspaceActivationVerificationError("Workspace activation admission fence conflicts with its journal")
    return admission


def validate_staged_terminal_outcome(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    target: TerminalActivationOutcome,
) -> None:
    _validate_staged_phase(operation, target=target)
    require_exact_admission_fence(db, operation)
    _validate_terminal_audit(db, operation, target=target)


def verify_exact_terminal_outcome(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    store: GitAgentVersionStore,
) -> ExactTerminalActivation:
    target = _require_terminal_operation_shape(operation)
    _validate_terminal_audit(db, operation, target=target)
    _require_released_admission_fence(db, operation)
    try:
        require_no_active_runtime_work(db, agent_id=operation.agent_id)
    except SessionConflictError as exc:
        raise WorkspaceActivationVerificationError("Workspace activation terminal outcome conflicts with active runtime work") from exc
    try:
        if target == "completed":
            verify_candidate(
                operation,
                store,
                allow_missing_historical_objects=True,
            )
        else:
            verify_rejected_workspace(operation, store)
    except WorkspaceActivationVerificationError:
        raise
    except Exception as exc:
        raise WorkspaceActivationVerificationError("Workspace activation terminal Workspace evidence is inconsistent") from exc
    try:
        verify_workspace_operation_refs(
            store,
            operation.operation_id,
            expected={},
            exact=True,
        )
    except GitCommandError as exc:
        raise WorkspaceActivationVerificationError("Workspace activation terminal outcome retained durable refs") from exc
    return ExactTerminalActivation(operation.operation_id, target)


def _validate_terminal_audit(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    target: TerminalActivationOutcome,
) -> None:
    if operation.action == "restore":
        if operation.import_id is not None:
            raise WorkspaceActivationVerificationError("Workspace restore activation must not own an import audit identity")
        return
    if not operation.import_id:
        raise WorkspaceActivationVerificationError("Workspace import activation has no import identity")
    audit = db.get(AgentWorkspaceImportRecordModel, operation.import_id)
    if audit is None:
        raise WorkspaceActivationVerificationError("Workspace import terminal audit is missing")
    if target == "completed":
        validate_accepted_import_audit(audit, operation)
        if dict(audit.error_json or {}) or list(audit.warnings_json or []) or not audit.completed_at:
            raise WorkspaceActivationVerificationError("Workspace accepted audit conflicts with its terminal outcome")
        return
    error_code, detail = _rejection_failure(operation)
    validate_rejected_import_audit(
        audit,
        operation,
        error_code=error_code,
        detail=detail,
    )
    if list(audit.warnings_json or []) or audit.created_at != operation.created_at or not audit.completed_at:
        raise WorkspaceActivationVerificationError("Workspace rejected audit conflicts with its terminal outcome")


def require_no_active_runtime_work(db: Session, *, agent_id: str) -> None:
    now = utc_now()
    active_session = db.scalar(
        select(
            exists().where(
                SessionRecordModel.agent_id == agent_id,
                SessionRecordModel.active_run_id.is_not(None),
                or_(
                    SessionRecordModel.active_run_expires_at.is_(None),
                    SessionRecordModel.active_run_expires_at > now,
                ),
            )
        )
    )
    active_turn = db.scalar(
        select(
            exists().where(
                SessionTurnIntentModel.agent_id == agent_id,
                SessionTurnIntentModel.status == "running",
            )
        )
    )
    waiting_hitl = db.scalar(
        select(
            exists().where(
                ClaudeUserInputRequestModel.business_agent_id == agent_id,
                ClaudeUserInputRequestModel.status == "waiting",
            )
        )
    )
    active_test = db.scalar(
        select(
            exists().where(
                AgentTestRunModel.agent_id == agent_id,
                AgentTestRunModel.status.in_(("queued", "running")),
            )
        )
    )
    if active_session or active_turn or waiting_hitl or active_test:
        raise SessionConflictError(f"Agent {agent_id} has active session, turn, user-input, or test work")


def _require_terminal_operation_shape(
    operation: AgentWorkspaceActivationOperationModel,
) -> TerminalActivationOutcome:
    if operation.state not in {"completed", "rejected"}:
        raise WorkspaceActivationVerificationError("Workspace activation is not terminal")
    target: TerminalActivationOutcome = "completed" if operation.state == "completed" else "rejected"
    expected_phase = "completion_outcome" if target == "completed" else "rejection_outcome"
    if operation.recovery_phase != expected_phase:
        raise WorkspaceActivationVerificationError("Workspace activation terminal phase conflicts with its outcome")
    required_identity = (
        operation.operation_id,
        operation.agent_id,
        operation.original_head_sha,
        operation.original_index_fingerprint,
        operation.original_workspace_fingerprint,
        operation.maintenance_token,
        operation.maintenance_expires_at,
        operation.created_at,
        operation.updated_at,
        operation.completed_at,
    )
    if any(not value for value in required_identity) or operation.original_index_snapshot is None:
        raise WorkspaceActivationVerificationError("Workspace activation terminal journal is incomplete")
    if operation.updated_at != operation.completed_at or operation.maintenance_generation < 1:
        raise WorkspaceActivationVerificationError("Workspace activation terminal metadata is incomplete")
    journal_fields = (
        operation.base_commit_sha,
        operation.candidate_commit_sha,
        operation.candidate_tree_sha,
        operation.original_index_tree_sha,
    )
    journal_complete = all(journal_fields)
    journal_empty = not any(journal_fields)
    if target == "completed" and not journal_complete:
        raise WorkspaceActivationVerificationError("Completed Workspace activation journal is incomplete")
    if target == "rejected" and not (journal_complete or journal_empty):
        raise WorkspaceActivationVerificationError("Rejected Workspace activation journal is partial")
    _require_terminal_action_shape(operation, target=target, journal_complete=journal_complete)
    if target == "completed" and dict(operation.error_json or {}):
        raise WorkspaceActivationVerificationError("Completed Workspace activation retained failure evidence")
    if target == "rejected":
        _rejection_failure(operation)
    return target


def _require_terminal_action_shape(
    operation: AgentWorkspaceActivationOperationModel,
    *,
    target: TerminalActivationOutcome,
    journal_complete: bool,
) -> None:
    if operation.action == "restore":
        import_values = (
            operation.import_id,
            operation.package_sha256,
            operation.tree_sha256,
            operation.suite_status,
        )
        if any(value is not None for value in import_values) or dict(operation.suite_json or {}) or list(operation.diagnostics_json or []):
            raise WorkspaceActivationVerificationError("Workspace restore terminal journal contains import evidence")
        if journal_complete and not operation.target_commit_sha:
            raise WorkspaceActivationVerificationError("Workspace restore terminal journal has no target")
        return
    if operation.action not in {"import_overwrite", "import_unchanged"}:
        raise WorkspaceActivationVerificationError("Workspace activation terminal action is invalid")
    if not operation.import_id or not operation.package_sha256 or not operation.tree_sha256 or operation.target_commit_sha is not None:
        raise WorkspaceActivationVerificationError("Workspace import terminal journal is incomplete")
    if target == "completed" and operation.suite_status is None:
        raise WorkspaceActivationVerificationError("Completed Workspace import has no suite outcome")


def _require_released_admission_fence(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
) -> None:
    admission = db.get(AgentAdmissionStateModel, operation.agent_id)
    expected = (
        None,
        operation.maintenance_generation,
        operation.maintenance_generation,
        None,
        None,
        None,
    )
    actual = (
        admission.maintenance_token if admission else None,
        admission.maintenance_generation if admission else None,
        admission.generation if admission else None,
        admission.maintenance_kind if admission else None,
        admission.maintenance_owner_id if admission else None,
        admission.maintenance_expires_at if admission else None,
    )
    if admission is None or actual != expected:
        raise WorkspaceActivationVerificationError("Workspace activation terminal admission release conflicts with its journal")


def _validate_staged_phase(
    operation: AgentWorkspaceActivationOperationModel,
    *,
    target: TerminalActivationOutcome,
) -> None:
    expected_phase = "completion_outcome" if target == "completed" else "rejection_outcome"
    expected_state = "completing" if target == "completed" else "rejecting"
    if operation.recovery_phase != expected_phase or operation.state not in {
        expected_state,
        "recovery_required",
    }:
        raise WorkspaceActivationVerificationError(f"Workspace activation has no staged {target} outcome")


def _maintenance_kind(operation: AgentWorkspaceActivationOperationModel) -> str:
    return "workspace_restore" if operation.action == "restore" else "workspace_import"


def _rejection_failure(
    operation: AgentWorkspaceActivationOperationModel,
) -> tuple[str, str]:
    error = dict(operation.error_json or {})
    error_code = error.get("error_code")
    detail = error.get("detail")
    if not isinstance(error_code, str) or not error_code or not isinstance(detail, str) or not detail:
        raise WorkspaceActivationVerificationError("Workspace rejected outcome has no exact failure identity")
    return error_code, detail
