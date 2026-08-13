from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, business_agent_repository_lock_path, validate_agent_id
from app.runtime.business_agent_lifecycle import business_agent_mutation_precondition
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID

ErrorFactory = Callable[[int, str], Exception]


def normalize_governed_agent_id(agent_id: str | None, *, error_factory: ErrorFactory) -> str:
    normalized = (agent_id or DEFAULT_BUSINESS_AGENT_ID).strip()
    try:
        return validate_agent_id(normalized)
    except InvalidAgentId as exc:
        raise error_factory(400, f"Invalid agent_id for version governance: {agent_id!r}") from exc


def resolve_existing_agent_store(
    *,
    stores: dict[str, GitAgentVersionStore],
    data_dir: Path,
    agent_id: str | None,
    agent_exists: Callable[[str], bool] | None,
    instance_etag: Callable[[str], str | None] | None,
    session_factory: sessionmaker | None,
    error_factory: ErrorFactory,
) -> GitAgentVersionStore:
    normalized = normalize_governed_agent_id(agent_id, error_factory=error_factory)
    existing = stores.get(normalized)
    if existing is not None:
        return existing
    if agent_exists is not None and not agent_exists(normalized):
        raise error_factory(404, f"Agent not registered for version governance: {normalized}")
    expected_etag = instance_etag(normalized) if instance_etag is not None else None
    if instance_etag is not None and expected_etag is None:
        raise error_factory(404, f"Agent instance not available for version governance: {normalized}")
    mutation_precondition = _mutation_precondition(
        session_factory=session_factory,
        agent_id=normalized,
        expected_etag=expected_etag,
        instance_etag=instance_etag,
        allow_workspace_activation=False,
    )
    activation_precondition = _mutation_precondition(
        session_factory=session_factory,
        agent_id=normalized,
        expected_etag=expected_etag,
        instance_etag=instance_etag,
        allow_workspace_activation=True,
    )
    layout = business_agent_layout(data_dir, normalized)
    store = GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
        repository_name=f"{normalized}-config",
        process_lock_path=business_agent_repository_lock_path(data_dir, normalized),
        mutation_precondition=mutation_precondition,
        activation_precondition=activation_precondition,
    )
    stores[normalized] = store
    return store


def _mutation_precondition(
    *,
    session_factory: sessionmaker | None,
    agent_id: str,
    expected_etag: str | None,
    instance_etag: Callable[[str], str | None] | None,
    allow_workspace_activation: bool,
) -> Callable[[], bool] | None:
    if session_factory is not None and expected_etag is not None:
        return business_agent_mutation_precondition(
            session_factory,
            agent_id=agent_id,
            expected_instance_etag=expected_etag,
            allow_workspace_activation=allow_workspace_activation,
        )
    if instance_etag is None:
        return None
    return lambda: instance_etag(agent_id) == expected_etag
