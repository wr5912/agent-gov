from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, cast

from sqlalchemy.orm import sessionmaker

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.workspace_activation_recovery import (
    WorkspaceActivationReconciliationAuthority,
    workspace_activation_is_reconcilable,
)
from app.services.agent_workspace_activation_contracts import (
    ActivationResolution,
    WorkspaceActivationFailure,
    WorkspaceActivationVerificationError,
)
from app.services.agent_workspace_activation_journal import touch_reconciliation
from app.services.agent_workspace_git_operations import (
    cleanup_workspace_operation_temporary_files,
    git_text,
)

logger = logging.getLogger(__name__)


class WorkspaceActivationReconciliationPort(Protocol):
    _Session: sessionmaker
    _store_for: Callable[[str], GitAgentVersionStore]

    def _require_operation(self, operation_id: str) -> AgentWorkspaceActivationOperationModel: ...

    def _reconciliation_is_authorized(
        self,
        operation_id: str,
        authority: WorkspaceActivationReconciliationAuthority | None,
    ) -> bool: ...

    def _complete_under_guard(self, operation_id: str, store: GitAgentVersionStore) -> None: ...

    def _finish_rejection_under_guard(
        self,
        operation_id: str,
        store: GitAgentVersionStore,
    ) -> ActivationResolution: ...

    def _reject_under_guard(
        self,
        operation_id: str,
        store: GitAgentVersionStore,
        *,
        failure: WorkspaceActivationFailure,
    ) -> ActivationResolution: ...

    def _reconcile_candidate(
        self,
        operation_id: str,
        store: GitAgentVersionStore,
    ) -> ActivationResolution: ...

    def _mark_recovery_required(
        self,
        operation_id: str,
        cause: Exception,
        *,
        failure: WorkspaceActivationFailure,
    ) -> ActivationResolution: ...


def reconcile_workspace_activation_operation(
    service: WorkspaceActivationReconciliationPort,
    operation_id: str,
    *,
    force: bool,
    cutoff: str,
    authority: WorkspaceActivationReconciliationAuthority | None,
) -> ActivationResolution:
    try:
        operation = service._require_operation(operation_id)
        if operation.state in {"completed", "rejected"}:
            return cast(ActivationResolution, operation.state)
        if not service._reconciliation_is_authorized(operation_id, authority):
            return "deferred"
        if (
            authority is None
            and not force
            and not workspace_activation_is_reconcilable(
                service._Session,
                operation,
                cutoff=cutoff,
            )
        ):
            try:
                touch_reconciliation(service._Session, operation_id)
            except Exception:
                logger.exception(
                    "event=workspace_activation.reconciliation_rotation_failed operation_id=%s",
                    operation_id,
                )
                return "persistence_failed"
            return "deferred"
        store = service._store_for(operation.agent_id)
        with store.workspace_activation_guard():
            operation = service._require_operation(operation_id)
            if operation.state in {"completed", "rejected"}:
                return cast(ActivationResolution, operation.state)
            if not service._reconciliation_is_authorized(operation_id, authority):
                return "deferred"
            if operation.state == "completing":
                service._complete_under_guard(operation_id, store)
                return "completed"
            if operation.state == "rejecting":
                return service._finish_rejection_under_guard(operation_id, store)
            cleanup_workspace_operation_temporary_files(store, operation_id, include_refs=False)
            if operation.state == "preparing" or not (operation.base_commit_sha and operation.candidate_commit_sha):
                return service._reject_under_guard(
                    operation_id,
                    store,
                    failure=_interrupted("Workspace activation stopped while its candidate was being prepared."),
                )
            head = git_text(store.repository_dir, ["rev-parse", "HEAD"]).strip()
            if head == operation.candidate_commit_sha:
                return service._reconcile_candidate(operation_id, store)
            if head in {operation.original_head_sha, operation.base_commit_sha}:
                return service._reject_under_guard(
                    operation_id,
                    store,
                    failure=_interrupted("Workspace activation stopped before the candidate was finalized."),
                )
            raise WorkspaceActivationVerificationError("Workspace HEAD is outside the durable activation journal")
    except Exception as exc:  # noqa: BLE001 - isolate one corrupt operation and retain its fence.
        return service._mark_recovery_required(
            operation_id,
            exc,
            failure=WorkspaceActivationFailure(
                "WORKSPACE_ACTIVATION_RECOVERY_REQUIRED",
                "Workspace activation requires deterministic recovery before this Agent can run.",
            ),
        )


def _interrupted(detail: str) -> WorkspaceActivationFailure:
    return WorkspaceActivationFailure("WORKSPACE_ACTIVATION_INTERRUPTED", detail)
