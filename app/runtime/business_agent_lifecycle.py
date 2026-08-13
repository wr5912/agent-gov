from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.agent_deletion_db import AgentDeletionOperationModel
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.business_agent_identity import business_agent_instance_etag
from app.runtime.runtime_db import AgentWorkspaceActivationOperationModel
from app.runtime.state_machines import WORKSPACE_ACTIVATION_FENCE_STATES


class BusinessAgentLifecycleFenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class BusinessAgentMutationPrecondition:
    """Read-only exact-instance fence evaluated under the stable Agent lock."""

    session_factory: sessionmaker
    agent_id: str
    expected_instance_etag: str
    allow_workspace_activation: bool = False

    def __call__(self) -> bool:
        try:
            with self.session_factory() as db:
                row = db.get(AgentRegistryModel, self.agent_id)
                if not _is_exact_public_instance(row, self.expected_instance_etag):
                    return False
                if _has_pending_deletion(db, agent_id=self.agent_id):
                    return False
                return self.allow_workspace_activation or not _has_workspace_activation_fence(
                    db,
                    agent_id=self.agent_id,
                )
        except SQLAlchemyError:
            return False


def business_agent_mutation_precondition(
    session_factory: sessionmaker,
    *,
    agent_id: str,
    expected_instance_etag: str,
    allow_workspace_activation: bool = False,
) -> BusinessAgentMutationPrecondition:
    return BusinessAgentMutationPrecondition(
        session_factory=session_factory,
        agent_id=agent_id,
        expected_instance_etag=expected_instance_etag,
        allow_workspace_activation=allow_workspace_activation,
    )


def require_public_business_agent(db: Session, *, agent_id: str) -> AgentRegistryModel:
    """在调用方写事务内校验 Agent 仍公开且没有 durable deletion fence。"""

    row = db.get(AgentRegistryModel, agent_id)
    if row is None or row.deleted_at or row.provision_state != "ready" or _has_pending_deletion(db, agent_id=agent_id):
        raise BusinessAgentLifecycleFenceError(f"Business Agent is not available for new work: {agent_id}")
    return row


def public_business_agent_instance_etag(db: Session, *, agent_id: str) -> str:
    """Return the exact current public instance identity or fail closed."""

    row = require_public_business_agent(db, agent_id=agent_id)
    if not row.provision_completed_token:
        raise BusinessAgentLifecycleFenceError(f"Business Agent public instance identity is unavailable: {agent_id}")
    return business_agent_instance_etag(row.provision_completed_token)


def require_exact_public_business_agent(
    db: Session,
    *,
    agent_id: str,
    expected_instance_etag: str,
) -> AgentRegistryModel:
    """Fence new work to the caller-observed public Agent instance."""

    row = require_public_business_agent(db, agent_id=agent_id)
    if not _is_exact_public_instance(row, expected_instance_etag):
        raise BusinessAgentLifecycleFenceError(f"Business Agent instance changed before new work was admitted: {agent_id}")
    return row


def _is_exact_public_instance(row: AgentRegistryModel | None, expected_instance_etag: str) -> bool:
    return bool(
        row is not None
        and not row.deleted_at
        and row.provision_state == "ready"
        and row.provision_completed_token
        and business_agent_instance_etag(row.provision_completed_token) == expected_instance_etag
    )


def _has_pending_deletion(db: Session, *, agent_id: str) -> bool:
    return (
        db.scalar(
            select(AgentDeletionOperationModel.operation_id)
            .where(
                AgentDeletionOperationModel.agent_id == agent_id,
                AgentDeletionOperationModel.state == "cleanup_pending",
            )
            .limit(1)
        )
        is not None
    )


def _has_workspace_activation_fence(db: Session, *, agent_id: str) -> bool:
    return (
        db.scalar(
            select(AgentWorkspaceActivationOperationModel.operation_id)
            .where(
                AgentWorkspaceActivationOperationModel.agent_id == agent_id,
                AgentWorkspaceActivationOperationModel.state.in_(WORKSPACE_ACTIVATION_FENCE_STATES),
            )
            .limit(1)
        )
        is not None
    )
