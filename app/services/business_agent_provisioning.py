from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from app.runtime.business_agent_workspace import (
    WorkspaceProvisioningError,
    WorkspaceProvisionJournal,
    WorkspaceProvisionPlan,
    apply_business_agent_workspace_plan,
    rollback_business_agent_workspace,
)
from app.runtime.errors import ConflictError, DataIntegrityError, FeedbackStoreError
from app.runtime.stores.agent_registry_store import (
    AgentProvisionReservation,
    AgentRegistryRecord,
    AgentRegistryStore,
)


class BusinessAgentProvisioningFailure(RuntimeError):
    """An owned provisioning attempt failed and was compensated completely."""


class BusinessAgentWorkspaceProvisioningConflict(ConflictError):
    """The requested Workspace could not be applied under its exact filesystem claim."""


def provision_business_agent(
    *,
    store: AgentRegistryStore,
    agent_id: str,
    name: str,
    workspace_dir: Path,
    plan: WorkspaceProvisionPlan,
    validate_workspace: Callable[[Path], None] | None = None,
    finalize_workspace: Callable[[Path], None] | None = None,
    rollback_workspace_finalization: Callable[[Path], bool] | None = None,
    prepare_publication: Callable[[], Callable[[Session], None]] | None = None,
) -> AgentRegistryRecord:
    """Coordinate DB reservation, safe Workspace apply and DB finalization."""
    reservation = store.reserve_business_agent(
        name=name,
        agent_id=agent_id,
        workspace_dir=str(workspace_dir),
    )
    journal: WorkspaceProvisionJournal | None = None
    try:
        journal = apply_business_agent_workspace_plan(
            workspace_dir,
            plan,
            require_workspace_absent=reservation.require_workspace_absent,
        )
        if validate_workspace is not None:
            validate_workspace(workspace_dir)
        if finalize_workspace is not None:
            finalize_workspace(workspace_dir)
        store.renew_business_agent_provision(reservation)
        publication_mutation = prepare_publication() if prepare_publication is not None else None
        if publication_mutation is not None:
            # Candidate inspection may invoke Git/materialization. Renew after
            # that out-of-transaction preparation, then keep publication short.
            store.renew_business_agent_provision(reservation)
            return store.finalize_business_agent(
                reservation,
                transaction_mutation=publication_mutation,
            )
        return store.finalize_business_agent(reservation)
    except Exception as exc:
        completed = _resolve_finalization_outcome(store, reservation, workspace_dir, exc)
        if completed is not None:
            return completed
        finalization_cleanup_complete = True
        if rollback_workspace_finalization is not None:
            try:
                finalization_cleanup_complete = rollback_workspace_finalization(workspace_dir)
            except Exception:
                finalization_cleanup_complete = False
        cleanup_complete = _rollback_after_failure(journal, exc) and finalization_cleanup_complete
        # Canonical new and recovery-marked attempts own an initially absent root. A
        # remaining root is unknown residue. A legacy non-canonical create owns only
        # the Workspace path established by its exact provisioning journal.
        if reservation.require_workspace_absent:
            cleanup_complete = cleanup_complete and not _path_exists_no_follow(workspace_dir.parent)
        elif reservation.created_new:
            cleanup_complete = cleanup_complete and not _path_exists_no_follow(workspace_dir)
        try:
            store.compensate_business_agent(
                reservation,
                workspace_cleanup_complete=cleanup_complete,
            )
        except Exception as compensation_error:
            raise DataIntegrityError(f"Business Agent provisioning compensation failed: {agent_id}") from compensation_error
        if isinstance(exc, WorkspaceProvisioningError):
            raise BusinessAgentWorkspaceProvisioningConflict("Business Agent workspace could not be provisioned safely") from exc
        if isinstance(exc, FeedbackStoreError):
            raise
        raise BusinessAgentProvisioningFailure(f"Business Agent provisioning failed: {agent_id}") from exc


def _resolve_finalization_outcome(
    store: AgentRegistryStore,
    reservation: AgentProvisionReservation,
    workspace_dir: Path,
    original_error: Exception,
) -> AgentRegistryRecord | None:
    try:
        outcome = store.resolve_business_agent_provision(reservation)
    except Exception as exc:
        raise DataIntegrityError(f"Business Agent provisioning outcome could not be determined; workspace preserved: {reservation.agent_id}") from exc
    if outcome.state == "completed":
        if outcome.record is None or not _path_exists_no_follow(workspace_dir):
            raise DataIntegrityError(f"Business Agent provisioning committed without a verifiable workspace: {reservation.agent_id}") from original_error
        return outcome.record
    if outcome.state == "owned":
        return None
    raise DataIntegrityError(f"Business Agent provisioning ownership could not be proven; workspace preserved: {reservation.agent_id}") from original_error


def _rollback_after_failure(
    journal: WorkspaceProvisionJournal | None,
    error: Exception,
) -> bool:
    if journal is not None:
        return rollback_business_agent_workspace(journal)
    if isinstance(error, WorkspaceProvisioningError):
        return error.cleanup_complete
    return True


def _path_exists_no_follow(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True
