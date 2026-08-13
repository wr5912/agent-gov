from __future__ import annotations

import os
import shutil

from app.agent_testing.service import AgentTestingService, PreparedWorkspaceImportAudit
from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_governance_schemas import agent_summary_response
from app.runtime.agent_paths import business_agent_layout, business_agent_repository_lock_path
from app.runtime.agent_workspace_package_schemas import WorkspaceImportResponse
from app.runtime.business_agent_workspace import WorkspaceProvisionPlan
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentIdentityReservedError, AgentRegistryStore
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_create_import import (
    ImportedRepositoryCandidate,
    cleanup_imported_repository,
    initialize_imported_repository,
    prepare_create_import_transaction,
)
from app.services.agent_workspace_package_results import build_import_response
from app.services.business_agent_provisioning import BusinessAgentWorkspaceProvisioningConflict, provision_business_agent


def create_workspace_from_package(
    *,
    settings: AppSettings,
    registry: AgentRegistryStore,
    agent_testing: AgentTestingService,
    agent_id: str,
    name: str,
    package: package_codec.ValidatedWorkspacePackage,
) -> WorkspaceImportResponse:
    with advisory_lock(business_agent_repository_lock_path(settings.data_dir, agent_id), mode="exclusive"):
        return _create_locked(
            settings=settings,
            registry=registry,
            agent_testing=agent_testing,
            agent_id=agent_id,
            name=name,
            package=package,
        )


def _create_locked(
    *,
    settings: AppSettings,
    registry: AgentRegistryStore,
    agent_testing: AgentTestingService,
    agent_id: str,
    name: str,
    package: package_codec.ValidatedWorkspacePackage,
) -> WorkspaceImportResponse:
    if shutil.which("git") is None:
        raise package_codec.WorkspacePackageError(503, "WORKSPACE_GIT_UNAVAILABLE", "git executable is not available")
    layout = business_agent_layout(settings.data_dir, agent_id)
    if _path_exists_no_follow(layout.root):
        raise _residue_error(agent_id)
    candidates: list[ImportedRepositoryCandidate] = []
    prepared_audits: list[PreparedWorkspaceImportAudit] = []
    try:
        record = provision_business_agent(
            store=registry,
            agent_id=agent_id,
            name=name,
            workspace_dir=layout.workspace,
            plan=WorkspaceProvisionPlan(entries=package.entries),
            finalize_workspace=lambda _: candidates.append(initialize_imported_repository(_new_store(settings, agent_id))),
            rollback_workspace_finalization=lambda _: _cleanup_created_repository(candidates),
            prepare_publication=lambda: prepare_create_import_transaction(
                agent_testing=agent_testing,
                agent_id=agent_id,
                package=package,
                candidates=candidates,
                prepared_audits=prepared_audits,
            ),
        )
    except AgentIdentityReservedError as exc:
        raise package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_AGENT_ID_RESERVED",
            f"Business Agent id {agent_id} is permanently reserved and cannot be imported again.",
        ) from exc
    except BusinessAgentWorkspaceProvisioningConflict as exc:
        raise package_codec.WorkspacePackageError(
            409,
            "WORKSPACE_IMPORT_PROVISIONING_FAILED",
            "Workspace package could not be applied safely; nothing was published.",
        ) from exc
    if len(candidates) != 1 or len(prepared_audits) != 1:
        raise package_codec.WorkspacePackageError(503, "WORKSPACE_IMPORT_VERSION_INIT_FAILED", "Imported workspace has no publication evidence")
    return build_import_response(
        action="created",
        agent=agent_summary_response(record),
        previous_commit_sha=None,
        current_commit_sha=candidates[0].commit_sha,
        package_sha256=package.package_sha256,
        tree_sha256=package.tree_sha256,
        rollback_target_commit_sha=None,
        prepared_audit=prepared_audits[0],
    )


def _new_store(settings: AppSettings, agent_id: str) -> GitAgentVersionStore:
    layout = business_agent_layout(settings.data_dir, agent_id)
    return GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
        repository_name=f"{agent_id}-config",
        git_user_name=settings.agent_git_user_name,
        git_user_email=settings.agent_git_user_email,
        process_lock_path=business_agent_repository_lock_path(settings.data_dir, agent_id),
    )


def _cleanup_created_repository(candidates: list[ImportedRepositoryCandidate]) -> bool:
    if not candidates:
        return True
    return len(candidates) == 1 and cleanup_imported_repository(candidates[0])


def _path_exists_no_follow(path: os.PathLike[str]) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _residue_error(agent_id: str) -> package_codec.WorkspacePackageError:
    return package_codec.WorkspacePackageError(
        409,
        "WORKSPACE_IMPORT_RESIDUE",
        f"Agent layout already exists for unregistered Agent {agent_id}; clean or restore the entire layout before import",
    )
