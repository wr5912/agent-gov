from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from scripts.bootstrap_runtime_volume import BootstrapResult, bootstrap_runtime_volume

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import (
    InvalidAgentId,
    business_agent_layout,
    business_agent_repository_lock_path,
    business_agents_root,
    validate_agent_id,
)
from app.runtime.business_agent_identity import business_agent_instance_etag
from app.runtime.managed_agent_policy import (
    WorkspacePolicyPlan,
    plan_workspace_policy,
    policy_projection,
    raise_for_policy_violations,
)
from app.runtime.state_machines import WORKSPACE_ACTIVATION_FENCE_STATES


class RuntimeSettingsView(Protocol):
    data_dir: Path
    runtime_volume_mode: str
    runtime_db_path: Path
    agent_git_user_name: str
    agent_git_user_email: str


class RuntimeInitializationError(RuntimeError):
    """Raised when startup state cannot be prepared without rewriting workspaces."""


def runtime_root_for_data_dir(data_dir: Path) -> Path:
    resolved = data_dir.resolve()
    if resolved == Path("/data"):
        return Path("/")
    if resolved.name != "data":
        raise RuntimeInitializationError(f"DATA_DIR must end in /data: {resolved}")
    return resolved.parent


def _registry_agent_id_sets(db_path: Path) -> tuple[set[str], set[str]]:
    if not db_path.is_file():
        return set(), set()
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "agent_registry" not in tables:
                return set(), set()
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_registry)")}
            if "agent_id" not in columns:
                return set(), set()
            fenced = _fenced_agent_ids(connection, tables=tables, columns=columns)
            required_visibility_columns = {
                "deleted_at",
                "provision_state",
                "provision_completed_token",
            }
            if not required_visibility_columns.issubset(columns):
                return set(), fenced
            active = {
                str(row[0])
                for row in connection.execute(
                    "SELECT agent_id FROM agent_registry "
                    "WHERE deleted_at IS NULL "
                    "AND provision_state = 'ready' "
                    "AND provision_completed_token IS NOT NULL "
                    "AND TRIM(provision_completed_token) != ''"
                )
            }
            return active - fenced, fenced
    except sqlite3.Error as exc:
        raise RuntimeInitializationError(f"Cannot inspect Agent registry: {exc.__class__.__name__}") from exc


def _fenced_agent_ids(connection: sqlite3.Connection, *, tables: set[str], columns: set[str]) -> set[str]:
    fenced: set[str] = set()
    if "deleted_at" in columns:
        fenced.update(str(row[0]) for row in connection.execute("SELECT agent_id FROM agent_registry WHERE deleted_at IS NOT NULL"))
    if "provision_state" in columns:
        fenced.update(
            str(row[0]) for row in connection.execute("SELECT agent_id FROM agent_registry WHERE provision_state IS NULL OR provision_state != 'ready'")
        )
    if "agent_deletion_operations" in tables:
        fenced.update(str(row[0]) for row in connection.execute("SELECT agent_id FROM agent_deletion_operations WHERE state = 'cleanup_pending'"))
    if "agent_workspace_activation_operations" in tables:
        states = tuple(sorted(WORKSPACE_ACTIVATION_FENCE_STATES))
        placeholders = ", ".join("?" for _ in states)
        fenced.update(
            str(row[0])
            for row in connection.execute(
                f"SELECT agent_id FROM agent_workspace_activation_operations WHERE state IN ({placeholders})",
                states,
            )
        )
    return fenced


def _registry_instance_etag(db_path: Path, agent_id: str) -> str | None:
    if not db_path.is_file():
        return None
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT provision_completed_token FROM agent_registry WHERE agent_id = ? AND deleted_at IS NULL AND provision_state = 'ready'",
                (agent_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    token = str(row[0]) if row is not None and row[0] else None
    return business_agent_instance_etag(token) if token else None


def _runtime_agent_ids(settings: RuntimeSettingsView) -> list[str]:
    active, _ = _registry_agent_id_sets(settings.runtime_db_path)
    validated: list[str] = []
    for raw_agent_id in sorted(active):
        try:
            agent_id = validate_agent_id(raw_agent_id)
        except InvalidAgentId:
            continue
        if business_agent_layout(settings.data_dir, agent_id).workspace.is_dir():
            validated.append(agent_id)
    return validated


def _policy_agent_ids(settings: RuntimeSettingsView) -> list[str]:
    """Discover read-only policy targets without granting Git mutation authority.

    A fresh bootstrap precedes registry sync, so its Workspace must still be
    validated.  Durable deletion and incomplete provisioning rows remain
    explicit visibility fences and are never inspected as public Workspaces.
    """

    _, fenced = _registry_agent_id_sets(settings.runtime_db_path)
    root = business_agents_root(settings.data_dir)
    if root.is_symlink() or not root.is_dir():
        return []
    discovered: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            agent_id = validate_agent_id(child.name)
        except InvalidAgentId:
            continue
        layout = business_agent_layout(settings.data_dir, agent_id)
        if agent_id in fenced or layout.workspace.is_symlink() or not layout.workspace.is_dir():
            continue
        discovered.append(agent_id)
    return discovered


def plan_runtime_policy(
    *,
    settings: RuntimeSettingsView,
    env: Mapping[str, str],
) -> tuple[WorkspacePolicyPlan, ...]:
    del env
    return tuple(
        plan_workspace_policy(
            workspace=business_agent_layout(settings.data_dir, agent_id).workspace,
            agent_id=agent_id,
        )
        for agent_id in _policy_agent_ids(settings)
    )


def validate_runtime_policy(
    *,
    settings: RuntimeSettingsView,
    env: Mapping[str, str],
) -> tuple[bool, str, tuple[WorkspacePolicyPlan, ...]]:
    plans = plan_runtime_policy(settings=settings, env=env)
    return all(plan.is_compliant for plan in plans), policy_projection(plans), plans


def _store_for(settings: RuntimeSettingsView, agent_id: str) -> GitAgentVersionStore:
    layout = business_agent_layout(settings.data_dir, agent_id)
    expected_etag = _registry_instance_etag(settings.runtime_db_path, agent_id)
    return GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
        repository_name=f"{agent_id}-config",
        git_user_name=settings.agent_git_user_name,
        git_user_email=settings.agent_git_user_email,
        process_lock_path=business_agent_repository_lock_path(settings.data_dir, agent_id),
        mutation_precondition=lambda: (
            expected_etag is not None
            and agent_id not in _registry_agent_id_sets(settings.runtime_db_path)[1]
            and _registry_instance_etag(settings.runtime_db_path, agent_id) == expected_etag
        ),
    )


def ensure_agent_repositories(settings: RuntimeSettingsView) -> None:
    """显式在各 Agent stable lock 与实例前置条件内补齐 Git 初始化。"""

    for agent_id in _runtime_agent_ids(settings):
        _store_for(settings, agent_id).ensure_bootstrap()


def prepare_runtime(
    *,
    settings: RuntimeSettingsView,
    bootstrap_dir: Path,
    env: Mapping[str, str],
    coordination_dir: Path,
) -> BootstrapResult:
    """Bootstrap missing files, validate live workspaces and refresh runtime evidence.

    Existing business-Agent Workspace bytes are never reconciled with the initialization source and
    startup never creates a managed-policy migration commit.
    """

    coordination_dir.mkdir(parents=True, exist_ok=True)
    bootstrap = bootstrap_runtime_volume(
        runtime_root=runtime_root_for_data_dir(settings.data_dir),
        bootstrap_dir=bootstrap_dir,
        runtime_volume_mode=settings.runtime_volume_mode,
        env=dict(env),
    )
    plans = plan_runtime_policy(settings=settings, env=env)
    raise_for_policy_violations(item for plan in plans for item in plan.violations)
    ensure_agent_repositories(settings)
    return bootstrap
