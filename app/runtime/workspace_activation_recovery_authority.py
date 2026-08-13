from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.runtime.advisory_lock import AdvisoryLockError, advisory_lock
from app.runtime.agent_git_errors import AgentGitError
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.agent_paths import business_agent_repository_lock_path
from app.runtime.recovery_cli_support import OperatorRecoveryError, normalize_operation_id


def resolve_workspace_activation_agent_id(
    session_factory: sessionmaker,
    operation_id: str,
) -> str:
    safe_operation_id = normalize_operation_id(operation_id)
    with session_factory() as db:
        agent_id = db.scalar(
            select(AgentWorkspaceActivationOperationModel.agent_id).where(
                AgentWorkspaceActivationOperationModel.operation_id == safe_operation_id,
            )
        )
    if agent_id is None:
        raise OperatorRecoveryError(
            "ACTIVATION_OPERATION_NOT_FOUND",
            "Workspace activation operation was not found",
        )
    return agent_id


@contextmanager
def workspace_activation_recovery_authority(
    store: GitAgentVersionStore,
    data_dir: Path,
    agent_id: str,
) -> Iterator[bool]:
    """按 store guard 的锁顺序取权；layout 缺失时退到同一外置 stable lock。"""

    guard = ExitStack()
    try:
        guard.enter_context(store.workspace_activation_guard())
    except AgentGitError:
        guard.close()
    else:
        with guard:
            yield True
        return

    lock_path = business_agent_repository_lock_path(data_dir, agent_id)
    try:
        with advisory_lock(lock_path, mode="exclusive"):
            yield False
    except AdvisoryLockError:
        raise OperatorRecoveryError(
            "RECOVERY_LOCK_UNAVAILABLE",
            "Workspace activation recovery authority is unavailable",
        ) from None
