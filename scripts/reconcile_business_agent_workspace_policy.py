#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime.agent_paths import validate_agent_id  # noqa: E402

try:
    from scripts.bootstrap_runtime_volume import DEFAULT_ENV_FILE, DEFAULT_TEMPLATE_DIR, resolve_runtime_root
    from scripts.runtime_template_renderer import RuntimeTemplateRenderContext, build_render_context, render_template_file
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from bootstrap_runtime_volume import DEFAULT_ENV_FILE, DEFAULT_TEMPLATE_DIR, resolve_runtime_root
    from runtime_template_renderer import RuntimeTemplateRenderContext, build_render_context, render_template_file

WorkspacePolicyObject = dict[str, object]

POLICY_RELATIVE_PATH = Path("workspace-policy/business-agent-policy.json")
HOOK_RELATIVE_PATH = Path("workspace-policy/pre_tool_guard.py")
AUDIT_RELATIVE_PATH = Path("data/transcripts/business-agent-workspace-policy-migration.jsonl")
BACKUP_RELATIVE_PATH = Path("data/.workspace-policy-backups")
LOCK_RELATIVE_PATH = Path("data/.workspace-policy-migration.lock")


@dataclass(frozen=True)
class CanonicalWorkspacePolicy:
    version: str
    sha256: str
    settings: WorkspacePolicyObject
    agent_settings: dict[str, WorkspacePolicyObject]
    hook_text: str


class _MigrationChangeRequired(TypedDict):
    agent_id: str
    kind: str
    path: str
    before_sha256: str
    after_sha256: str
    before: str
    after: str
    existed: bool
    anchor: str


class MigrationChange(_MigrationChangeRequired, total=False):
    backup: str


class _MigrationChangeSummaryRequired(TypedDict):
    agent_id: str
    kind: str
    path: str
    before_sha256: str
    after_sha256: str


class MigrationChangeSummary(_MigrationChangeSummaryRequired, total=False):
    backup: str


class MigrationResult(TypedDict):
    ok: bool
    dry_run: bool
    runtime_root: str
    policy_version: str
    policy_sha256: str
    backup_root: str | None
    changes: list[MigrationChangeSummary]


def reconcile_business_agent_workspace_policy(
    *,
    runtime_root: Path,
    template_dir: Path,
    env_file: Path,
    runtime_volume_mode: str | None,
    apply: bool,
    backup_dir: Path | None = None,
    operator: str = "system",
) -> MigrationResult:
    runtime_root = runtime_root.resolve()
    env = _load_env(env_file)
    mode = runtime_volume_mode or _mode_from_env_file(env_file)
    context = build_render_context(mode=mode, env=env, runtime_root=runtime_root)
    policy = _load_canonical_policy(template_dir, context)
    data_dir = runtime_root / "data"
    _assert_real_directory(data_dir, label="runtime data directory")
    lock_path = runtime_root / LOCK_RELATIVE_PATH
    with _exclusive_lock(lock_path, anchor=data_dir):
        if apply:
            _harden_private_migration_state(runtime_root)
        try:
            return _reconcile_locked(
                runtime_root=runtime_root,
                template_dir=template_dir,
                context=context,
                policy=policy,
                apply=apply,
                backup_dir=backup_dir,
                operator=operator,
            )
        finally:
            if apply:
                _harden_private_migration_state(runtime_root)


def _reconcile_locked(
    *,
    runtime_root: Path,
    template_dir: Path,
    context: RuntimeTemplateRenderContext,
    policy: CanonicalWorkspacePolicy,
    apply: bool,
    backup_dir: Path | None,
    operator: str,
) -> MigrationResult:
    changes = _planned_changes(runtime_root=runtime_root, template_dir=template_dir, context=context, policy=policy)
    data_dir = runtime_root / "data"
    backup_root = backup_dir or runtime_root / BACKUP_RELATIVE_PATH / f"{_timestamp()}-{policy.sha256[:12]}"
    _require_within(backup_root, data_dir, label="workspace policy backup directory")
    if apply and changes:
        attempted: list[MigrationChange] = []
        try:
            for change in changes:
                backup = _backup_change(change, runtime_root=runtime_root, backups_root=backup_root, anchor=data_dir)
                if backup is not None:
                    change["backup"] = backup.as_posix()
            for change in changes:
                attempted.append(change)
                _atomic_write_text(Path(change["path"]), change["after"], anchor=Path(change["anchor"]))
            _write_event(runtime_root, changes, policy=policy, operator=operator, status="completed")
        except Exception as exc:
            rollback_errors = _rollback_changes(attempted)
            try:
                _write_event(
                    runtime_root,
                    changes,
                    policy=policy,
                    operator=operator,
                    status="failed",
                    error=exc,
                    rollback_errors=rollback_errors,
                )
            except Exception as audit_exc:
                raise RuntimeError(
                    f"Workspace policy migration failed with {exc.__class__.__name__}; failure audit also failed with {audit_exc.__class__.__name__}"
                ) from exc
            if rollback_errors:
                raise RuntimeError(f"Workspace policy migration failed and rollback had {len(rollback_errors)} error(s)") from exc
            raise
    return {
        "ok": True,
        "dry_run": not apply,
        "runtime_root": runtime_root.as_posix(),
        "policy_version": policy.version,
        "policy_sha256": policy.sha256,
        "backup_root": backup_root.as_posix() if changes else None,
        "changes": [_public_change(change) for change in changes],
    }


def _planned_changes(
    *,
    runtime_root: Path,
    template_dir: Path,
    context: RuntimeTemplateRenderContext,
    policy: CanonicalWorkspacePolicy,
) -> list[MigrationChange]:
    changes: list[MigrationChange] = []
    agents_dir = runtime_root / "data" / "business-agents"
    if _lstat(agents_dir) is None:
        return changes
    _assert_real_directory(agents_dir, label="business agents directory")
    for agent_dir in sorted(agents_dir.iterdir()):
        agent_stat = _lstat(agent_dir)
        if agent_stat is None:
            continue
        if stat.S_ISLNK(agent_stat.st_mode):
            raise ValueError(f"Business Agent path must not be a symlink: {agent_dir}")
        if not stat.S_ISDIR(agent_stat.st_mode):
            continue
        agent_id = validate_agent_id(agent_dir.name)
        workspace = agent_dir / "workspace"
        if _lstat(workspace) is None:
            continue
        _assert_real_directory(workspace, label=f"business Agent {agent_id} workspace")
        settings_path = workspace / ".claude" / "settings.json"
        hook_path = workspace / "hooks" / "pre_tool_guard.py"
        _validate_workspace_target(settings_path, workspace=workspace)
        _validate_workspace_target(hook_path, workspace=workspace)
        current_settings = _current_settings_text(
            settings_path=settings_path,
            agent_id=agent_id,
            template_dir=template_dir,
            context=context,
        )
        _append_change(
            changes,
            agent_id,
            settings_path,
            "settings_native_policy",
            _merge_settings(current_settings, _settings_for_agent(policy, agent_id)),
            anchor=workspace,
        )
        _append_change(
            changes,
            agent_id,
            hook_path,
            "pre_tool_guard",
            policy.hook_text,
            anchor=workspace,
        )
    return changes


def _current_settings_text(
    *,
    settings_path: Path,
    agent_id: str,
    template_dir: Path,
    context: RuntimeTemplateRenderContext,
) -> str:
    existing = _read_optional_text_no_follow(settings_path, anchor=settings_path.parents[1])
    if existing is not None:
        return existing
    seeded = template_dir / "data" / "business-agents" / agent_id / "workspace" / ".claude" / "settings.json"
    general = template_dir / "templates" / "business-agent" / "general" / ".claude" / "settings.json"
    source = seeded if seeded.is_file() else general
    if not source.is_file():
        raise FileNotFoundError(f"No settings template for business Agent {agent_id}: {source}")
    return render_template_file(
        source.read_text(encoding="utf-8"),
        rel_path=Path(".claude/settings.json"),
        context=context,
    )


def _load_canonical_policy(template_dir: Path, context: RuntimeTemplateRenderContext) -> CanonicalWorkspacePolicy:
    policy_path = template_dir / POLICY_RELATIVE_PATH
    hook_path = template_dir / HOOK_RELATIVE_PATH
    policy_text = policy_path.read_text(encoding="utf-8")
    hook_text = hook_path.read_text(encoding="utf-8")
    rendered = render_template_file(policy_text, rel_path=Path(".claude/settings.json"), context=context)
    payload = _json_object(rendered, label=policy_path.as_posix())
    version = payload.get("version")
    settings = payload.get("settings")
    agent_settings = payload.get("agentSettings", {})
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{policy_path} must define a non-empty string version")
    if not isinstance(settings, dict):
        raise ValueError(f"{policy_path} must define an object settings patch")
    if not isinstance(agent_settings, dict) or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in agent_settings.items()):
        raise ValueError(f"{policy_path} agentSettings must map Agent ids to objects")
    for agent_id in agent_settings:
        validate_agent_id(agent_id)
    digest = hashlib.sha256((policy_text + "\0" + hook_text).encode("utf-8")).hexdigest()
    return CanonicalWorkspacePolicy(
        version=version,
        sha256=digest,
        settings=settings,
        agent_settings=agent_settings,
        hook_text=hook_text,
    )


def _settings_for_agent(policy: CanonicalWorkspacePolicy, agent_id: str) -> WorkspacePolicyObject:
    merged = json.loads(json.dumps(policy.settings, ensure_ascii=False))
    if not isinstance(merged, dict):  # pragma: no cover - validated before serialization
        raise ValueError("canonical workspace settings must be an object")
    overlay = policy.agent_settings.get(agent_id)
    if overlay is not None:
        _merge_policy_object(merged, overlay)
    return merged


def _merge_policy_object(target: WorkspacePolicyObject, overlay: WorkspacePolicyObject) -> None:
    for key, value in overlay.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_policy_object(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged = list(current)
            merged.extend(item for item in value if item not in merged)
            target[key] = merged
        else:
            target[key] = value


def _merge_settings(text: str, patch: WorkspacePolicyObject) -> str:
    data = _json_object(text, label="workspace settings")
    permissions = _object_field(data, "permissions")
    policy_permissions = _required_object(patch, "permissions")
    for key in ("defaultMode", "disableBypassPermissionsMode"):
        permissions[key] = policy_permissions[key]
    permissions["deny"] = _merge_string_lists(permissions.get("deny"), policy_permissions.get("deny"), "permissions.deny")

    hooks = _object_field(data, "hooks")
    policy_hooks = _required_object(patch, "hooks")
    existing_pre = _object_list(hooks.get("PreToolUse"), "hooks.PreToolUse")
    canonical_pre = _object_list(policy_hooks.get("PreToolUse"), "policy hooks.PreToolUse")
    hooks["PreToolUse"] = [*canonical_pre, *(entry for entry in existing_pre if not _matches_bash(entry))]

    sandbox = _object_field(data, "sandbox")
    policy_sandbox = _required_object(patch, "sandbox")
    for key in ("enabled", "failIfUnavailable", "autoAllowBashIfSandboxed", "allowUnsandboxedCommands"):
        sandbox[key] = policy_sandbox[key]
    filesystem = _object_field(sandbox, "filesystem")
    policy_filesystem = _required_object(policy_sandbox, "filesystem")
    filesystem["denyRead"] = _merge_string_lists(
        filesystem.get("denyRead"),
        policy_filesystem.get("denyRead"),
        "sandbox.filesystem.denyRead",
    )
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _json_object(text: str, *, label: str) -> WorkspacePolicyObject:
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return loaded


def _object_field(parent: WorkspacePolicyObject, key: str) -> WorkspacePolicyObject:
    value = parent.get(key)
    if value is None:
        created: dict[str, object] = {}
        parent[key] = created
        return created
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_object(parent: WorkspacePolicyObject, key: str) -> WorkspacePolicyObject:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"canonical policy {key} must be an object")
    return value


def _object_list(value: object, label: str) -> list[WorkspacePolicyObject]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a list of objects")
    return list(value)


def _merge_string_lists(current: object, required: object, label: str) -> list[str]:
    if current is None:
        current = []
    if not isinstance(current, list) or any(not isinstance(item, str) for item in current):
        raise ValueError(f"{label} must be a list of strings")
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError(f"canonical policy {label} must be a list of strings")
    return list(dict.fromkeys([*current, *required]))


def _matches_bash(entry: dict[str, object]) -> bool:
    matcher = entry.get("matcher")
    return isinstance(matcher, str) and "Bash" in matcher.split("|")


def _validate_workspace_target(path: Path, *, workspace: Path) -> None:
    _require_within(path, workspace, label="business Agent workspace target")
    _ensure_safe_directory_chain(workspace, path.parent, create=False)
    target_stat = _lstat(path)
    if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
        raise ValueError(f"Business Agent workspace target must be a regular file: {path}")


def _read_optional_text_no_follow(path: Path, *, anchor: Path) -> str | None:
    _require_within(path, anchor, label="workspace policy input")
    _ensure_safe_directory_chain(anchor, path.parent, create=False)
    target_stat = _lstat(path)
    if target_stat is None:
        return None
    if not stat.S_ISREG(target_stat.st_mode):
        raise ValueError(f"Workspace policy input must be a regular file: {path}")
    directory_fd = _open_directory_chain(anchor, path.parent)
    try:
        file_fd = os.open(path.name, os.O_RDONLY | _no_follow_flag(), dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError(f"Workspace policy input must be a regular file: {path}")
            with os.fdopen(file_fd, "r", encoding="utf-8", closefd=False) as fh:
                return fh.read()
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _ensure_safe_directory_chain(anchor: Path, target: Path, *, create: bool) -> None:
    _assert_real_directory(anchor, label="workspace policy path anchor")
    relative = _require_within(target, anchor, label="workspace policy directory")
    current = anchor
    for part in relative.parts:
        current = current / part
        current_stat = _lstat(current)
        if current_stat is None:
            if not create:
                return
            current.mkdir(mode=0o700)
            current_stat = _lstat(current)
        if current_stat is None or not stat.S_ISDIR(current_stat.st_mode):
            raise ValueError(f"Workspace policy directory must not be a symlink or non-directory: {current}")


def _assert_real_directory(path: Path, *, label: str) -> None:
    path_stat = _lstat(path)
    if path_stat is None or not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"{label} must be a real directory: {path}")


def _require_within(path: Path, anchor: Path, *, label: str) -> Path:
    try:
        return path.relative_to(anchor)
    except ValueError as exc:
        raise ValueError(f"{label} escapes its allowed root: {path}") from exc


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _open_directory_chain(anchor: Path, target: Path) -> int:
    relative = _require_within(target, anchor, label="workspace policy directory")
    current_fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag())
    try:
        for part in relative.parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _stat_at(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _no_follow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(value, int) or value == 0:  # pragma: no cover - Linux container invariant
        raise RuntimeError("O_NOFOLLOW is required for workspace policy migration")
    return value


def _append_change(
    changes: list[MigrationChange],
    agent_id: str,
    path: Path,
    kind: str,
    after: str,
    *,
    anchor: Path,
) -> None:
    existing = _read_optional_text_no_follow(path, anchor=anchor)
    existed = existing is not None
    before = existing or ""
    if before == after:
        return
    changes.append(
        {
            "agent_id": agent_id,
            "kind": kind,
            "path": path.as_posix(),
            "before_sha256": _sha256(before),
            "after_sha256": _sha256(after),
            "before": before,
            "after": after,
            "existed": existed,
            "anchor": anchor.as_posix(),
        }
    )


def _backup_change(
    change: MigrationChange,
    *,
    runtime_root: Path,
    backups_root: Path,
    anchor: Path,
) -> Path | None:
    if not change["existed"]:
        return None
    path = Path(change["path"])
    rel = path.relative_to(runtime_root)
    backup = backups_root / rel
    _atomic_write_text(backup, change["before"], anchor=anchor)
    return backup


def _rollback_changes(changes: list[MigrationChange]) -> list[str]:
    errors: list[str] = []
    for change in reversed(changes):
        try:
            path = Path(change["path"])
            anchor = Path(change["anchor"])
            if change["existed"]:
                _atomic_write_text(path, change["before"], anchor=anchor)
            else:
                _unlink_regular_file(path, anchor=anchor)
        except Exception as exc:  # pragma: no cover - separately reported as a fatal rollback failure
            errors.append(exc.__class__.__name__)
    return errors


def _atomic_write_text(path: Path, content: str, *, anchor: Path) -> None:
    _require_within(path, anchor, label="workspace policy write target")
    _ensure_safe_directory_chain(anchor, path.parent, create=True)
    directory_fd = _open_directory_chain(anchor, path.parent)
    temp_name = f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        target_stat = _stat_at(directory_fd, path.name)
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            raise ValueError(f"Workspace policy target must be a regular file: {path}")
        mode = target_stat.st_mode & 0o777 if target_stat is not None else 0o600
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag()
        temp_fd = os.open(temp_name, flags, mode, dir_fd=directory_fd)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=directory_fd)
        os.close(directory_fd)


def _unlink_regular_file(path: Path, *, anchor: Path) -> None:
    _require_within(path, anchor, label="workspace policy rollback target")
    if _lstat(path) is None:
        return
    _ensure_safe_directory_chain(anchor, path.parent, create=False)
    directory_fd = _open_directory_chain(anchor, path.parent)
    try:
        target_stat = _stat_at(directory_fd, path.name)
        if target_stat is None:
            return
        if not stat.S_ISREG(target_stat.st_mode):
            raise ValueError(f"Workspace policy rollback target must be a regular file: {path}")
        os.unlink(path.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def _exclusive_lock(path: Path, *, anchor: Path) -> Iterator[None]:
    _require_within(path, anchor, label="workspace policy lock")
    _ensure_safe_directory_chain(anchor, path.parent, create=True)
    directory_fd = _open_directory_chain(anchor, path.parent)
    try:
        lock_fd = os.open(path.name, os.O_RDWR | os.O_CREAT | _no_follow_flag(), 0o600, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError(f"Workspace policy lock must be a regular file: {path}")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def _write_event(
    runtime_root: Path,
    changes: list[MigrationChange],
    *,
    policy: CanonicalWorkspacePolicy,
    operator: str,
    status: str,
    error: Exception | None = None,
    rollback_errors: list[str] | None = None,
) -> None:
    event_path = runtime_root / AUDIT_RELATIVE_PATH
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "operator": operator,
        "policy_version": policy.version,
        "policy_sha256": policy.sha256,
        "change_count": len(changes),
        "changes": [_public_change(change) for change in changes],
    }
    if error is not None:
        record["error_type"] = error.__class__.__name__
        record["rollback_ok"] = not rollback_errors
        record["rollback_error_types"] = rollback_errors or []
    _append_json_line(event_path, record, anchor=runtime_root / "data")


def _append_json_line(path: Path, record: dict[str, object], *, anchor: Path) -> None:
    _require_within(path, anchor, label="workspace policy audit path")
    _ensure_safe_directory_chain(anchor, path.parent, create=True)
    directory_fd = _open_directory_chain(anchor, path.parent)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _no_follow_flag()
    try:
        audit_fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(audit_fd).st_mode):
                raise ValueError(f"Workspace policy audit target must be a regular file: {path}")
            os.fchmod(audit_fd, 0o600)
            payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(audit_fd, view)
                if written <= 0:  # pragma: no cover - os.write either progresses or raises
                    raise OSError("workspace policy audit write made no progress")
                view = view[written:]
            os.fsync(audit_fd)
        finally:
            os.close(audit_fd)
    finally:
        os.close(directory_fd)


def _harden_private_migration_state(runtime_root: Path) -> None:
    data_dir = runtime_root / "data"
    backup_root = runtime_root / BACKUP_RELATIVE_PATH
    if _lstat(backup_root) is not None:
        _assert_real_directory(backup_root, label="workspace policy backup directory")
        backup_fd = _open_directory_chain(data_dir, backup_root)
        try:
            _harden_private_directory_fd(backup_fd, backup_root)
        finally:
            os.close(backup_fd)
    _harden_private_file(runtime_root / AUDIT_RELATIVE_PATH, anchor=data_dir)


def _harden_private_directory_fd(directory_fd: int, path: Path) -> None:
    os.fchmod(directory_fd, 0o700)
    for name in os.listdir(directory_fd):
        value_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(value_stat.st_mode):
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag(), dir_fd=directory_fd)
            try:
                _harden_private_directory_fd(child_fd, path / name)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(value_stat.st_mode):
            raise ValueError(f"Workspace policy private state must contain only directories and regular files: {path / name}")
        file_fd = os.open(name, os.O_RDONLY | _no_follow_flag(), dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError(f"Workspace policy private state changed while hardening: {path / name}")
            os.fchmod(file_fd, 0o600)
        finally:
            os.close(file_fd)


def _harden_private_file(path: Path, *, anchor: Path) -> None:
    if _lstat(path) is None:
        return
    _ensure_safe_directory_chain(anchor, path.parent, create=False)
    directory_fd = _open_directory_chain(anchor, path.parent)
    try:
        file_fd = os.open(path.name, os.O_RDONLY | _no_follow_flag(), dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError(f"Workspace policy private file must be regular: {path}")
            os.fchmod(file_fd, 0o600)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _public_change(change: MigrationChange) -> MigrationChangeSummary:
    summary: MigrationChangeSummary = {
        "agent_id": change["agent_id"],
        "kind": change["kind"],
        "path": change["path"],
        "before_sha256": change["before_sha256"],
        "after_sha256": change["after_sha256"],
    }
    if "backup" in change:
        summary["backup"] = change["backup"]
    return summary


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = os.path.expandvars(os.path.expanduser(value.strip().strip("'\"")))
    return env


def _mode_from_env_file(path: Path) -> str:
    return "local-debug" if "local-debug" in path.name else "container"


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile the canonical native policy into business-agent workspaces.")
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--runtime-volume-mode", choices=["container", "local-debug"], default=None)
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this flag the command is dry-run.")
    parser.add_argument("--operator", default="system")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file, args.runtime_volume_mode)
    result = reconcile_business_agent_workspace_policy(
        runtime_root=runtime_root,
        template_dir=args.template_dir,
        env_file=args.env_file,
        runtime_volume_mode=args.runtime_volume_mode,
        apply=args.apply,
        operator=args.operator,
    )
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
