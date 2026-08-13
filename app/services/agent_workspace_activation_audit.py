from __future__ import annotations

from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.services.agent_workspace_activation_contracts import (
    WorkspaceActivationVerificationError,
)


def validate_accepted_import_audit(
    audit: AgentWorkspaceImportRecordModel,
    operation: AgentWorkspaceActivationOperationModel,
) -> None:
    audit_action = "unchanged" if operation.action == "import_unchanged" else "overwritten"
    expected = (
        operation.agent_id,
        audit_action,
        "accepted",
        operation.package_sha256,
        operation.tree_sha256,
        operation.candidate_commit_sha,
        operation.suite_status,
        dict(operation.suite_json or {}),
        list(operation.diagnostics_json or []),
    )
    actual = (
        audit.agent_id,
        audit.action,
        audit.status,
        audit.package_sha256,
        audit.tree_sha256,
        audit.commit_sha,
        audit.suite_status,
        dict(audit.suite_json or {}),
        list(audit.diagnostics_json or []),
    )
    if actual != expected:
        raise WorkspaceActivationVerificationError("Workspace import audit conflicts with its activation journal")


def validate_rejected_import_audit(
    audit: AgentWorkspaceImportRecordModel,
    operation: AgentWorkspaceActivationOperationModel,
    *,
    error_code: str,
    detail: str,
) -> None:
    expected = (
        operation.agent_id,
        "overwrite",
        "failed",
        operation.package_sha256,
        operation.tree_sha256,
        None,
        {},
        None,
        [],
        {"error_code": error_code, "detail": detail},
    )
    actual = (
        audit.agent_id,
        audit.action,
        audit.status,
        audit.package_sha256,
        audit.tree_sha256,
        audit.commit_sha,
        dict(audit.suite_json or {}),
        audit.suite_status,
        list(audit.diagnostics_json or []),
        dict(audit.error_json or {}),
    )
    if actual != expected:
        raise WorkspaceActivationVerificationError("Workspace rejected audit conflicts with its activation journal")
