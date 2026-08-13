from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import sessionmaker

from app.agent_testing.service import PreparedWorkspaceImportAudit
from app.runtime.agent_admission import AgentMaintenanceClaim
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.runtime_db import utc_now
from app.runtime.state_machines import (
    WORKSPACE_ACTIVATION_FENCE_STATES,
    WORKSPACE_ACTIVATION_RECOVERY_PHASES,
    validate_transition,
)
from app.services.agent_workspace_activation_contracts import (
    ActivationAction,
    ActivationResolution,
    WorkspaceActivationError,
    WorkspaceActivationFailure,
    WorkspaceActivationPersistenceError,
    WorkspaceActivationVerificationError,
)
from app.services.agent_workspace_activation_refs import WorkspaceOperationRefs
from app.services.agent_workspace_git_evidence import index_fingerprint
from app.services.agent_workspace_git_operations import (
    SnapshotState,
    TreeReplacement,
    WorkspaceObservation,
    git_text,
    workspace_fingerprint,
    workspace_status,
)

TerminalResolution = Literal["completed", "rejected"]
RECOVERY_PHASE_TRANSITIONS = {
    "none": {"candidate_reset", "base_reset"},
    "candidate_reset": {"candidate_reset", "base_reset"},
    "base_reset": {"base_reset", "head_reset"},
    "head_reset": {"head_reset", "index_restore"},
    "index_restore": {"index_restore"},
    "completion_outcome": set(),
    "rejection_outcome": set(),
}
if set(RECOVERY_PHASE_TRANSITIONS) != WORKSPACE_ACTIVATION_RECOVERY_PHASES:
    raise RuntimeError("Workspace activation recovery phase transition table is incomplete")


def new_import_id() -> str:
    return f"awi-{uuid.uuid4()}"


def validate_activation_claim(
    agent_id: str,
    claim: AgentMaintenanceClaim,
    *,
    expected_kind: str,
) -> None:
    if claim.agent_id != agent_id or claim.kind != expected_kind:
        raise WorkspaceActivationVerificationError(f"Workspace activation claim must belong to {agent_id} and kind {expected_kind}")


def new_preparing_operation(
    *,
    agent_id: str,
    action: ActivationAction,
    observation: WorkspaceObservation,
    claim: AgentMaintenanceClaim,
    import_id: str | None,
    package_sha256: str | None,
    tree_sha256: str | None,
) -> AgentWorkspaceActivationOperationModel:
    now = utc_now()
    return AgentWorkspaceActivationOperationModel(
        operation_id=f"wao-{uuid.uuid4()}",
        import_id=import_id,
        agent_id=agent_id,
        action=action,
        state="preparing",
        original_head_sha=observation.original_head,
        base_commit_sha=None,
        candidate_commit_sha=None,
        candidate_tree_sha=None,
        target_commit_sha=None,
        snapshot_created=False,
        original_status_text=observation.original_status,
        original_index_fingerprint=observation.original_index_fingerprint,
        original_index_snapshot=observation.original_index_snapshot,
        original_workspace_fingerprint=observation.original_workspace_fingerprint,
        original_index_tree_sha=None,
        recovery_phase="none",
        package_sha256=package_sha256,
        tree_sha256=tree_sha256,
        suite_status=None,
        suite_json={},
        diagnostics_json=[],
        maintenance_token=claim.token,
        maintenance_generation=claim.generation,
        maintenance_expires_at=claim.expires_at,
        error_json={},
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def validate_preparation(
    operation: AgentWorkspaceActivationOperationModel,
    snapshot: SnapshotState,
    replacement: TreeReplacement,
    prepared_audit: PreparedWorkspaceImportAudit | None,
) -> None:
    if (
        snapshot.original_head != operation.original_head_sha
        or snapshot.original_status != operation.original_status_text
        or snapshot.original_index_fingerprint != operation.original_index_fingerprint
        or snapshot.original_index_snapshot != operation.original_index_snapshot
        or snapshot.original_workspace_fingerprint != operation.original_workspace_fingerprint
        or replacement.previous_commit_sha != snapshot.current_head
    ):
        raise WorkspaceActivationVerificationError("Workspace candidate does not match its preparing journal")
    if prepared_audit is not None and (
        prepared_audit.import_id != operation.import_id
        or prepared_audit.package_sha256 != operation.package_sha256
        or prepared_audit.tree_sha256 != operation.tree_sha256
        or prepared_audit.commit_sha != replacement.current_commit_sha
    ):
        raise WorkspaceActivationVerificationError("Workspace import audit does not match its preparing journal")


def snapshot_from_operation(operation: AgentWorkspaceActivationOperationModel) -> SnapshotState:
    if not operation.base_commit_sha or not operation.original_index_tree_sha or operation.original_index_snapshot is None:
        raise WorkspaceActivationVerificationError("Workspace activation snapshot journal is incomplete")
    return SnapshotState(
        original_head=operation.original_head_sha,
        current_head=operation.base_commit_sha,
        snapshot_created=operation.snapshot_created,
        original_status=operation.original_status_text,
        original_index_tree_sha=operation.original_index_tree_sha,
        original_index_fingerprint=operation.original_index_fingerprint,
        original_index_snapshot=operation.original_index_snapshot,
        original_workspace_fingerprint=operation.original_workspace_fingerprint,
    )


def required_sha(value: str | None, field: str) -> str:
    if not value:
        raise WorkspaceActivationVerificationError(f"Workspace activation {field} SHA is missing")
    return value


def durable_ref_values(operation: AgentWorkspaceActivationOperationModel) -> WorkspaceOperationRefs:
    expected: WorkspaceOperationRefs = {
        "original": operation.original_head_sha,
        "base": required_sha(operation.base_commit_sha, "base"),
        "candidate": required_sha(operation.candidate_commit_sha, "candidate"),
        "original-index-tree": required_sha(operation.original_index_tree_sha, "original index tree"),
    }
    if operation.target_commit_sha:
        expected["target"] = operation.target_commit_sha
    return expected


def verify_original_observation(
    operation: AgentWorkspaceActivationOperationModel,
    repository: Path,
) -> None:
    if git_text(repository, ["rev-parse", "HEAD"]).strip() != operation.original_head_sha:
        raise WorkspaceActivationVerificationError("Workspace HEAD changed while its candidate was being prepared")
    if workspace_status(repository) != operation.original_status_text:
        raise WorkspaceActivationVerificationError("Workspace status changed while its candidate was being prepared")
    if index_fingerprint(repository) != operation.original_index_fingerprint:
        raise WorkspaceActivationVerificationError("Workspace index changed while its candidate was being prepared")
    if workspace_fingerprint(repository) != operation.original_workspace_fingerprint:
        raise WorkspaceActivationVerificationError("Workspace bytes changed while its candidate was being prepared")


def terminal_resolution(session_factory: sessionmaker, operation_id: str) -> TerminalResolution | None:
    try:
        with session_factory() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
            if operation is not None and operation.state == "completed":
                return "completed"
            if operation is not None and operation.state == "rejected":
                return "rejected"
    except Exception:
        return None
    return None


def persist_recovery_required(
    session_factory: sessionmaker,
    operation_id: str,
    cause: Exception,
    *,
    failure: WorkspaceActivationFailure,
) -> ActivationResolution:
    persistence_error: Exception | None = None
    try:
        with session_factory.begin() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
            if operation is None:
                raise WorkspaceActivationPersistenceError("Workspace activation operation disappeared")
            if operation.state not in {"completed", "rejected"}:
                if operation.state != "recovery_required":
                    validate_transition("workspace_activation", operation.state, "recovery_required")
                    operation.state = "recovery_required"
                operation.error_json = {
                    "error_code": failure.error_code,
                    "detail": failure.detail,
                    "cause_type": cause.__class__.__name__,
                }
                operation.updated_at = utc_now()
    except Exception as exc:
        persistence_error = exc
    resolution = _fresh_recovery_resolution(session_factory, operation_id)
    if resolution is not None:
        return resolution
    if persistence_error is not None:
        raise WorkspaceActivationPersistenceError("Workspace activation recovery state could not be persisted") from persistence_error
    raise WorkspaceActivationPersistenceError("Workspace activation recovery state could not be verified")


def touch_reconciliation(session_factory: sessionmaker, operation_id: str) -> None:
    with session_factory.begin() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
        if operation is not None and operation.state in WORKSPACE_ACTIVATION_FENCE_STATES:
            operation.updated_at = utc_now()


def _fresh_recovery_resolution(
    session_factory: sessionmaker,
    operation_id: str,
) -> ActivationResolution | None:
    try:
        with session_factory() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
            if operation is None:
                raise WorkspaceActivationPersistenceError("Workspace activation operation disappeared")
            if operation.state == "completed":
                return "completed"
            if operation.state == "rejected":
                return "rejected"
            if operation.state == "recovery_required":
                return "recovery_required"
            return None
    except WorkspaceActivationError:
        raise
    except Exception as exc:
        raise WorkspaceActivationPersistenceError("Workspace activation recovery state could not be read") from exc


def set_recovery_phase(session_factory: sessionmaker, operation_id: str, phase: str) -> None:
    try:
        with session_factory.begin() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
            if operation is None or operation.state in {"completed", "rejected"}:
                raise WorkspaceActivationVerificationError("Workspace activation recovery phase is no longer writable")
            allowed = RECOVERY_PHASE_TRANSITIONS.get(operation.recovery_phase)
            if allowed is None or phase not in allowed:
                raise WorkspaceActivationVerificationError(f"Workspace activation recovery phase cannot move from {operation.recovery_phase} to {phase}")
            operation.recovery_phase = phase
            operation.updated_at = utc_now()
    except Exception:
        with session_factory() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, operation_id)
            if operation is None or operation.recovery_phase != phase:
                raise
