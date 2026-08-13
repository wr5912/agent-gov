from __future__ import annotations

import logging
from typing import Literal

from app.agent_testing.service import AgentTestingService, PreparedWorkspaceImportAudit
from app.runtime.agent_governance_schemas import AgentSummaryResponse
from app.runtime.agent_workspace_package_schemas import WorkspaceImportResponse
from app.services import agent_workspace_package_codec as package_codec


def build_import_response(
    *,
    action: Literal["created", "overwritten", "unchanged"],
    agent: AgentSummaryResponse,
    previous_commit_sha: str | None,
    current_commit_sha: str,
    package_sha256: str,
    tree_sha256: str,
    rollback_target_commit_sha: str | None,
    prepared_audit: PreparedWorkspaceImportAudit,
) -> WorkspaceImportResponse:
    return WorkspaceImportResponse(
        action=action,
        agent=agent,
        previous_commit_sha=previous_commit_sha,
        current_commit_sha=current_commit_sha,
        package_sha256=package_sha256,
        tree_sha256=tree_sha256,
        rollback_target_commit_sha=rollback_target_commit_sha,
        import_record_id=prepared_audit.import_id,
        test_suite_status=prepared_audit.suite_status,
        test_file_count=prepared_audit.suite.test_file_count,
        test_suite_diagnostics=prepared_audit.suite.diagnostics,
    )


def record_import_failure(
    *,
    agent_testing: AgentTestingService,
    logger: logging.Logger,
    agent_id: str,
    action: str,
    package: package_codec.ValidatedWorkspacePackage | None,
    error: package_codec.WorkspacePackageError,
) -> None:
    try:
        agent_testing.record_import_failure(
            agent_id=agent_id,
            action=action,
            package_sha256=package.package_sha256 if package else None,
            tree_sha256=package.tree_sha256 if package else None,
            error_code=error.error_code,
            detail=str(error),
        )
    except Exception:
        logger.warning(
            "Failed to persist Workspace import failure audit: agent_id=%s action=%s",
            agent_id,
            action,
            exc_info=True,
        )
