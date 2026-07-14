from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from scripts.bootstrap_runtime_volume import BootstrapResult, bootstrap_runtime_volume
from scripts.runtime_template_renderer import build_render_context

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.managed_agent_policy import (
    PolicyChange,
    WorkspacePolicyPlan,
    plan_workspace_policy,
    policy_projection,
    raise_for_policy_violations,
)

TERMINAL_CHANGE_SET_STATES = {"published", "rejected", "abandoned", "failed"}


class RuntimeSettingsView(Protocol):
    data_dir: Path
    runtime_volume_mode: str
    runtime_db_path: Path
    agent_git_user_name: str
    agent_git_user_email: str


class RuntimeInitializationError(RuntimeError):
    """Raised when startup-managed state cannot be prepared safely."""


@dataclass(frozen=True)
class RuntimePreparationResult:
    bootstrap: BootstrapResult
    agent_commits: Mapping[str, str]
    managed_output_digest: str


def runtime_root_for_data_dir(data_dir: Path) -> Path:
    resolved = data_dir.resolve()
    if resolved == Path("/data"):
        return Path("/")
    if resolved.name != "data":
        raise RuntimeInitializationError(f"DATA_DIR must end in /data: {resolved}")
    return resolved.parent


def _seed_agent_ids(template_dir: Path) -> set[str]:
    root = template_dir / "data" / "business-agents"
    if not root.is_dir():
        return set()
    return {path.name for path in root.iterdir() if (path / "workspace").is_dir()}


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


def _managed_agent_ids(settings: RuntimeSettingsView, template_dir: Path) -> list[str]:
    root = settings.data_dir / "business-agents"
    known = _seed_agent_ids(template_dir) | _active_registry_agent_ids(settings.runtime_db_path)
    return sorted(agent_id for agent_id in known if (root / agent_id / "workspace").is_dir())


def _template_workspace(template_dir: Path, agent_id: str) -> Path | None:
    path = template_dir / "data" / "business-agents" / agent_id / "workspace"
    return path if path.is_dir() else None


def plan_runtime_policy(
    *,
    settings: RuntimeSettingsView,
    template_dir: Path,
    env: Mapping[str, str],
) -> tuple[WorkspacePolicyPlan, ...]:
    runtime_root = runtime_root_for_data_dir(settings.data_dir)
    context = build_render_context(mode=settings.runtime_volume_mode, env=dict(env), runtime_root=runtime_root)
    plans: list[WorkspacePolicyPlan] = []
    for agent_id in _managed_agent_ids(settings, template_dir):
        workspace = business_agent_layout(settings.data_dir, agent_id).workspace
        plans.append(
            plan_workspace_policy(
                workspace=workspace,
                agent_id=agent_id,
                template_workspace=_template_workspace(template_dir, agent_id),
                render_context=context,
            )
        )
    return tuple(plans)


def validate_runtime_policy(
    *,
    settings: RuntimeSettingsView,
    template_dir: Path,
    env: Mapping[str, str],
) -> tuple[bool, str, tuple[WorkspacePolicyPlan, ...]]:
    plans = plan_runtime_policy(settings=settings, template_dir=template_dir, env=env)
    compliant = all(plan.is_compliant for plan in plans)
    return compliant, policy_projection(plans), plans


def _open_change_sets(db_path: Path, agent_id: str) -> list[str]:
    if not db_path.is_file():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_change_sets)")}
            if not {"change_set_id", "status"}.issubset(columns):
                return []
            if "agent_id" in columns:
                rows = connection.execute(
                    "SELECT change_set_id, status FROM agent_change_sets WHERE agent_id = ?",
                    (agent_id,),
                ).fetchall()
            elif agent_id == "main-agent":
                rows = connection.execute("SELECT change_set_id, status FROM agent_change_sets").fetchall()
            else:
                return []
    except sqlite3.Error as exc:
        raise RuntimeInitializationError(f"Cannot inspect open change sets: {exc.__class__.__name__}") from exc
    return [str(row[0]) for row in rows if str(row[1]) not in TERMINAL_CHANGE_SET_STATES]


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


def _ensure_managed_agent_repositories(settings: RuntimeSettingsView, template_dir: Path) -> None:
    for agent_id in _managed_agent_ids(settings, template_dir):
        _store_for(settings, agent_id).ensure_bootstrap()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_policy_changes(changes: tuple[PolicyChange, ...]) -> None:
    staged: list[tuple[PolicyChange, Path]] = []
    try:
        for change in changes:
            target = change.path
            if target.is_symlink() or not target.is_file():
                raise RuntimeInitializationError(f"Managed path changed type before apply: {target}")
            current = target.read_text(encoding="utf-8")
            if hashlib.sha256(current.encode("utf-8")).hexdigest() != change.before_sha256:
                raise RuntimeInitializationError(f"Managed path changed during migration: {target}")
            temporary = target.with_name(f".{target.name}.agentgov-{change.after_sha256[:12]}.tmp")
            temporary.unlink(missing_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, target.stat().st_mode & 0o777)
            try:
                os.write(descriptor, change.content.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            staged.append((change, temporary))
        for change, temporary in staged:
            os.replace(temporary, change.path)
            directory = os.open(change.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)


def _journal_payload(transaction_id: str, entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repositories": entries,
    }


def _safe_recover_journal(settings: RuntimeSettingsView, journal_path: Path) -> None:
    if not journal_path.is_file():
        return
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        entries = payload.get("repositories") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ValueError("invalid repositories")
        for item in reversed(entries):
            if not isinstance(item, dict):
                raise ValueError("invalid repository entry")
            agent_id = str(item["agent_id"])
            original_head = str(item["original_head"])
            store = _store_for(settings, agent_id)
            current = store.current_commit_sha()
            dirty_paths = {str(change["path"]) for change in store.workspace_changes()}
            planned_paths = {str(path) for path in item.get("paths", [])}
            planned_paths.update(str(path) for path in item.get("temporary_paths", []))
            if dirty_paths - planned_paths:
                raise RuntimeInitializationError(f"Migration recovery found unrelated dirty files for {agent_id}")
            if current != original_head or dirty_paths:
                store.reset_to_ref_for_managed_migration(original_head)
        journal_path.unlink()
    except RuntimeInitializationError:
        raise
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeInitializationError(f"Cannot recover managed migration journal: {exc.__class__.__name__}") from exc


def _apply_policy_migrations(
    *,
    settings: RuntimeSettingsView,
    plans: tuple[WorkspacePolicyPlan, ...],
    journal_path: Path,
) -> Mapping[str, str]:
    changing = [plan for plan in plans if plan.changes]
    if not changing:
        return {}
    entries: list[dict[str, object]] = []
    stores: dict[str, GitAgentVersionStore] = {}
    for plan in changing:
        open_change_sets = _open_change_sets(settings.runtime_db_path, plan.agent_id)
        if open_change_sets:
            raise RuntimeInitializationError(f"Open change sets block managed policy migration for {plan.agent_id}: {', '.join(open_change_sets)}")
        store = _store_for(settings, plan.agent_id)
        store.ensure_bootstrap()
        dirty = store.workspace_changes()
        if dirty:
            paths = ", ".join(sorted(str(item["path"]) for item in dirty))
            raise RuntimeInitializationError(f"Dirty workspace blocks managed policy migration for {plan.agent_id}: {paths}")
        head = store.current_commit_sha()
        if not head:
            raise RuntimeInitializationError(f"Agent repository has no HEAD: {plan.agent_id}")
        stores[plan.agent_id] = store
        entries.append(
            {
                "agent_id": plan.agent_id,
                "original_head": head,
                "paths": [change.path.relative_to(plan.workspace).as_posix() for change in plan.changes],
                "temporary_paths": [
                    change.path.with_name(f".{change.path.name}.agentgov-{change.after_sha256[:12]}.tmp").relative_to(plan.workspace).as_posix()
                    for change in plan.changes
                ],
            }
        )

    transaction_id = uuid.uuid4().hex
    _atomic_json(journal_path, _journal_payload(transaction_id, entries))
    commits: dict[str, str] = {}
    try:
        for plan in changing:
            _write_policy_changes(plan.changes)
            summary = stores[plan.agent_id].create_snapshot(
                reason="managed_policy_migration",
                note=f"AgentGov managed policy migration {transaction_id}",
            )
            commit = str(summary.get("commit_sha") or summary.get("agent_version_id") or "")
            if not commit:
                raise RuntimeInitializationError(f"Managed migration produced no commit: {plan.agent_id}")
            commits[plan.agent_id] = commit
        journal_path.unlink()
        return commits
    except Exception:
        for entry in reversed(entries):
            agent_id = str(entry["agent_id"])
            stores[agent_id].reset_to_ref_for_managed_migration(str(entry["original_head"]))
        journal_path.unlink(missing_ok=True)
        raise


def prepare_runtime(
    *,
    settings: RuntimeSettingsView,
    template_dir: Path,
    env: Mapping[str, str],
    coordination_dir: Path,
) -> RuntimePreparationResult:
    coordination_dir.mkdir(parents=True, exist_ok=True)
    journal_path = coordination_dir / "migration-journal.json"
    _safe_recover_journal(settings, journal_path)
    runtime_root = runtime_root_for_data_dir(settings.data_dir)
    bootstrap = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=template_dir,
        runtime_volume_mode=settings.runtime_volume_mode,
        env=dict(env),
    )
    if bootstrap["validation_errors"]:
        raise RuntimeInitializationError("; ".join(bootstrap["validation_errors"]))
    _ensure_managed_agent_repositories(settings, template_dir)
    plans = plan_runtime_policy(settings=settings, template_dir=template_dir, env=env)
    raise_for_policy_violations(item for plan in plans for item in plan.violations)
    commits = _apply_policy_migrations(settings=settings, plans=plans, journal_path=journal_path)
    compliant, output_digest, final_plans = validate_runtime_policy(settings=settings, template_dir=template_dir, env=env)
    if not compliant:
        details = [f"{item.agent_id}:{item.path}:{item.rule_id}" for plan in final_plans for item in plan.violations]
        details.extend(f"{plan.agent_id}:{plan.workspace.as_posix()}:managed_policy_drift" for plan in final_plans if plan.changes)
        raise RuntimeInitializationError("; ".join(details))
    return RuntimePreparationResult(bootstrap, commits, output_digest)
