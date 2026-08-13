from __future__ import annotations

import uuid

from app.agent_testing.models import AgentTestRunModel, AgentWorkspaceImportRecordModel
from app.runtime.agent_maintenance_db import (
    AgentAdmissionStateModel,
    AgentWorkspaceActivationOperationModel,
)
from app.runtime.runtime_db_base import utc_now
from app.runtime.workspace_activation_recovery import RecoveryAttemptRequest
from tests.workspace_activation_recovery_test_support import (
    RecoveryHarness,
    activation_service,
    git,
)


def stage_completed_terminal_for_resume(
    recovery_harness: RecoveryHarness,
    request: RecoveryAttemptRequest,
    *,
    mutation: str | None = None,
) -> None:
    if mutation == "status_only":
        with recovery_harness.Session.begin() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, request.operation_id)
            assert operation is not None
            operation.state = "completed"
            operation.completed_at = utc_now()
        return
    resolution = activation_service(recovery_harness).reconcile_exact_operation(
        request.operation_id,
        recovery_attempt_id=request.recovery_id,
        expected_state_digest=request.state_digest,
    )
    assert resolution == "completed"
    with recovery_harness.Session.begin() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, request.operation_id)
        assert operation is not None
        admission = db.get(AgentAdmissionStateModel, operation.agent_id)
        assert admission is not None
        if mutation == "audit":
            audit = db.get(AgentWorkspaceImportRecordModel, operation.import_id)
            assert audit is not None
            audit.package_sha256 = "f" * 64
        elif mutation == "admission":
            admission.maintenance_token = "residual-token"
            admission.maintenance_kind = "workspace_import"
            admission.maintenance_owner_id = operation.operation_id
            admission.maintenance_expires_at = operation.maintenance_expires_at
        elif mutation == "graph":
            operation.original_head_sha = str(operation.candidate_commit_sha)
        elif mutation == "active_test":
            db.add(
                AgentTestRunModel(
                    test_run_id=f"run-{uuid.uuid4()}",
                    agent_id=operation.agent_id,
                    commit_sha=str(operation.candidate_commit_sha),
                    source="manual",
                    status="queued",
                )
            )
    if mutation == "refs":
        with recovery_harness.Session() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, request.operation_id)
            assert operation is not None and operation.candidate_commit_sha is not None
            candidate = operation.candidate_commit_sha
        git(
            recovery_harness.workspace,
            "update-ref",
            f"refs/agentgov/workspace-activations/{request.operation_id}/candidate",
            candidate,
        )
    elif mutation == "workspace":
        recovery_harness.workspace.joinpath("terminal-drift.txt").write_text(
            "hostile drift\n",
            encoding="utf-8",
        )
    elif mutation in {"index_assume_unchanged", "index_skip_worktree"}:
        flag = {
            "index_assume_unchanged": "--assume-unchanged",
            "index_skip_worktree": "--skip-worktree",
        }[mutation]
        git(
            recovery_harness.workspace,
            "update-index",
            flag,
            "--",
            "CLAUDE.md",
        )
