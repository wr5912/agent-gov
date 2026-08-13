from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.runtime_db import utc_now
from app.services.agent_workspace_activation_audit import (
    validate_accepted_import_audit,
    validate_rejected_import_audit,
)
from app.services.agent_workspace_activation_contracts import (
    WorkspaceActivationAuditPersistenceError,
    WorkspaceActivationFailure,
    WorkspaceActivationVerificationError,
)


def persist_or_validate_accepted_import(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    persist_accepted_import: Callable[..., None],
) -> None:
    if operation.action not in {"import_overwrite", "import_unchanged"}:
        return
    if not operation.import_id:
        raise WorkspaceActivationVerificationError("Workspace import activation has no import identity")
    existing = db.get(AgentWorkspaceImportRecordModel, operation.import_id)
    if existing is not None:
        validate_accepted_import_audit(existing, operation)
        return
    audit_action = "unchanged" if operation.action == "import_unchanged" else "overwritten"
    try:
        persist_accepted_import(
            db,
            import_id=operation.import_id,
            agent_id=operation.agent_id,
            action=audit_action,
            package_sha256=operation.package_sha256,
            tree_sha256=operation.tree_sha256,
            commit_sha=operation.candidate_commit_sha,
            suite=dict(operation.suite_json or {}),
            suite_status=operation.suite_status,
            diagnostics=list(operation.diagnostics_json or []),
        )
    except Exception as exc:
        raise WorkspaceActivationAuditPersistenceError("Accepted Workspace import audit could not be persisted") from exc


def persist_or_validate_rejected_import(
    db: Session,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    failure: WorkspaceActivationFailure,
) -> None:
    if operation.action not in {"import_overwrite", "import_unchanged"}:
        return
    if not operation.import_id:
        raise WorkspaceActivationVerificationError("Workspace import activation has no import identity")
    existing = db.get(AgentWorkspaceImportRecordModel, operation.import_id)
    if existing is not None:
        validate_rejected_import_audit(
            existing,
            operation,
            error_code=failure.error_code,
            detail=failure.detail,
        )
        return
    now = utc_now()
    db.add(
        AgentWorkspaceImportRecordModel(
            import_id=operation.import_id,
            agent_id=operation.agent_id,
            action="overwrite",
            status="failed",
            package_sha256=operation.package_sha256,
            tree_sha256=operation.tree_sha256,
            commit_sha=None,
            created_at=operation.created_at,
            completed_at=now,
            suite_json={},
            suite_status=None,
            diagnostics_json=[],
            error_json={
                "error_code": failure.error_code,
                "detail": failure.detail,
            },
        )
    )
    db.flush()


def require_no_accepted_import_audit(
    session_factory: sessionmaker,
    operation: AgentWorkspaceActivationOperationModel,
) -> None:
    if not operation.import_id:
        return
    with session_factory() as db:
        audit = db.get(AgentWorkspaceImportRecordModel, operation.import_id)
        if audit is not None and audit.status == "accepted":
            raise WorkspaceActivationVerificationError("Accepted Workspace import audit forbids Git compensation")
