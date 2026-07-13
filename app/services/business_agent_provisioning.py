from __future__ import annotations

import os
from pathlib import Path

from app.runtime.business_agent_workspace import (
    WorkspaceProvisioningError,
    WorkspaceProvisionJournal,
    WorkspaceSafetyError,
    apply_business_agent_workspace_plan,
    prepare_business_agent_workspace,
    rollback_business_agent_workspace,
)
from app.runtime.errors import ConfigurationError, ConflictError, DataIntegrityError
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore


def provision_business_agent(
    *,
    store: AgentRegistryStore,
    agent_id: str,
    name: str,
    workspace_dir: Path,
    template_id: str,
) -> AgentRegistryRecord:
    """Coordinate preflight, DB reservation, FS apply and DB finalization."""
    try:
        plan = prepare_business_agent_workspace(
            agent_id=agent_id,
            name=name,
            template_id=template_id,
        )
    except WorkspaceSafetyError as exc:
        raise ConfigurationError("Business Agent template failed safety validation") from exc

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
        store.renew_business_agent_provision(reservation)
        return store.finalize_business_agent(reservation)
    except Exception as exc:
        cleanup_complete = _rollback_after_failure(journal, exc)
        # New and recovery-marked attempts own an initially absent root. A remaining
        # root is therefore unknown residue. Normal tombstone reuse may preserve its
        # pre-existing workspace when the owned-path rollback completed.
        if reservation.created_new or reservation.require_workspace_absent:
            cleanup_complete = cleanup_complete and not _path_exists_no_follow(workspace_dir)
        try:
            store.compensate_business_agent(
                reservation,
                workspace_cleanup_complete=cleanup_complete,
            )
        except Exception as compensation_error:
            raise DataIntegrityError(f"Business Agent provisioning compensation failed: {agent_id}") from compensation_error
        if isinstance(exc, WorkspaceProvisioningError):
            raise ConflictError("Business Agent workspace could not be provisioned safely") from exc
        raise


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
