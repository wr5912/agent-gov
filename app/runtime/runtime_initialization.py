from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from scripts.bootstrap_runtime_volume import BootstrapResult, bootstrap_runtime_volume

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.business_agent_seed_catalog import declared_business_agent_ids, runtime_seed_catalog_dir
from app.runtime.managed_agent_policy import (
    WorkspacePolicyPlan,
    plan_workspace_policy,
    policy_projection,
    raise_for_policy_violations,
)


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


def _active_registry_agent_ids(db_path: Path) -> set[str]:
    if not db_path.is_file():
        return set()
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_registry)")}
            if "agent_id" not in columns:
                return set()
            deleted_clause = " AND deleted_at IS NULL" if "deleted_at" in columns else ""
            rows = connection.execute(f"SELECT agent_id FROM agent_registry WHERE 1=1{deleted_clause}").fetchall()
            return {str(row[0]) for row in rows}
    except sqlite3.Error as exc:
        raise RuntimeInitializationError(f"Cannot inspect Agent registry: {exc.__class__.__name__}") from exc


def _runtime_agent_ids(settings: RuntimeSettingsView, template_dir: Path) -> list[str]:
    del template_dir  # 声明集读运行态 catalog，不读仓库出生配置（已删 seed 不再是候选）。
    root = settings.data_dir / "business-agents"
    catalog_root = runtime_seed_catalog_dir(settings.data_dir)
    known = set(declared_business_agent_ids(seed_root=catalog_root)) | _active_registry_agent_ids(settings.runtime_db_path)
    validated: list[str] = []
    for raw_agent_id in sorted(known):
        try:
            agent_id = validate_agent_id(raw_agent_id)
        except InvalidAgentId:
            continue
        if (root / agent_id / "workspace").is_dir():
            validated.append(agent_id)
    return validated


def plan_runtime_policy(
    *,
    settings: RuntimeSettingsView,
    template_dir: Path,
    env: Mapping[str, str],
) -> tuple[WorkspacePolicyPlan, ...]:
    del env
    return tuple(
        plan_workspace_policy(
            workspace=business_agent_layout(settings.data_dir, agent_id).workspace,
            agent_id=agent_id,
        )
        for agent_id in _runtime_agent_ids(settings, template_dir)
    )


def validate_runtime_policy(
    *,
    settings: RuntimeSettingsView,
    template_dir: Path,
    env: Mapping[str, str],
) -> tuple[bool, str, tuple[WorkspacePolicyPlan, ...]]:
    plans = plan_runtime_policy(settings=settings, template_dir=template_dir, env=env)
    return all(plan.is_compliant for plan in plans), policy_projection(plans), plans


def _store_for(settings: RuntimeSettingsView, agent_id: str) -> GitAgentVersionStore:
    layout = business_agent_layout(settings.data_dir, agent_id)
    return GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
        repository_name=f"{agent_id}-config",
        git_user_name=settings.agent_git_user_name,
        git_user_email=settings.agent_git_user_email,
    )


def _ensure_agent_repositories(settings: RuntimeSettingsView, template_dir: Path) -> None:
    for agent_id in _runtime_agent_ids(settings, template_dir):
        _store_for(settings, agent_id).ensure_bootstrap()


def prepare_runtime(
    *,
    settings: RuntimeSettingsView,
    template_dir: Path,
    env: Mapping[str, str],
    coordination_dir: Path,
) -> BootstrapResult:
    """Bootstrap missing files, validate live workspaces and refresh runtime evidence.

    Existing business-Agent workspace bytes are never reconciled with the seed and
    startup never creates a managed-policy migration commit.
    """

    coordination_dir.mkdir(parents=True, exist_ok=True)
    bootstrap = bootstrap_runtime_volume(
        runtime_root=runtime_root_for_data_dir(settings.data_dir),
        template_dir=template_dir,
        runtime_volume_mode=settings.runtime_volume_mode,
        env=dict(env),
    )
    plans = plan_runtime_policy(settings=settings, template_dir=template_dir, env=env)
    raise_for_policy_violations(item for plan in plans for item in plan.violations)
    _ensure_agent_repositories(settings, template_dir)
    return bootstrap
