from __future__ import annotations

from app.agent_testing.store import AgentTestingStore
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout, business_agent_repository_lock_path
from app.runtime.business_agent_lifecycle import business_agent_mutation_precondition
from app.runtime.runtime_db import make_session_factory
from app.runtime.sdk_session_store import clear_inactive_sdk_sessions_for_agent_in_transaction
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.services.agent_workspace_activation import WorkspaceActivationService
from app.services.agent_workspace_activation_recovery import (
    RecoveryOperatorContext,
    WorkspaceActivationOperatorRecoveryService,
)


def build_workspace_activation_recovery_runtime_service(
    settings: AppSettings,
) -> WorkspaceActivationOperatorRecoveryService:
    session_factory = make_session_factory(settings.runtime_db_path)
    registry = AgentRegistryStore(session_factory)
    stores: dict[str, GitAgentVersionStore] = {}

    def store_for(agent_id: str) -> GitAgentVersionStore:
        existing = stores.get(agent_id)
        if existing is not None:
            return existing
        record = registry.get_agent(agent_id)
        layout = business_agent_layout(settings.data_dir, agent_id)
        store = GitAgentVersionStore(
            repository_dir=layout.workspace,
            worktrees_dir=layout.version_base / "worktrees",
            releases_dir=layout.version_base / "releases",
            repository_name=f"{agent_id}-config",
            git_user_name=settings.agent_git_user_name,
            git_user_email=settings.agent_git_user_email,
            process_lock_path=business_agent_repository_lock_path(settings.data_dir, agent_id),
            mutation_precondition=(
                business_agent_mutation_precondition(
                    session_factory,
                    agent_id=agent_id,
                    expected_instance_etag=record.instance_etag,
                    allow_workspace_activation=True,
                )
                if record is not None
                else lambda: False
            ),
        )
        stores[agent_id] = store
        return store

    activation = WorkspaceActivationService(
        session_factory=session_factory,
        store_for=store_for,
        invalidate_sessions=lambda db, agent_id: clear_inactive_sdk_sessions_for_agent_in_transaction(
            db,
            agent_id=agent_id,
        ),
        persist_accepted_import=AgentTestingStore.record_import_in_transaction,
    )

    def reconcile_exact(context: RecoveryOperatorContext) -> str:
        return activation.reconcile_exact_operation(
            context.operation_id,
            recovery_attempt_id=context.recovery_id,
            expected_state_digest=context.expected_state_digest,
        )

    return WorkspaceActivationOperatorRecoveryService(
        session_factory=session_factory,
        data_dir=settings.data_dir,
        store_for=store_for,
        reconcile_exact=reconcile_exact,
    )
