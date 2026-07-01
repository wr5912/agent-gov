#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from collections.abc import Iterable, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

try:
    from scripts.runtime_cleanup import cleanup_runtime_artifacts
    from scripts.runtime_template_renderer import (
        RuntimeTemplateRenderContext,
        build_render_context,
        is_template_managed_text_file,
        render_template_file,
        validate_rendered_config,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from runtime_cleanup import cleanup_runtime_artifacts
    from runtime_template_renderer import (
        RuntimeTemplateRenderContext,
        build_render_context,
        is_template_managed_text_file,
        render_template_file,
        validate_rendered_config,
    )

DEFAULT_TEMPLATE_DIR = Path("docker/runtime-volume-seeds")
DEFAULT_ENV_FILE = Path("docker/.env")
CONTAINER_RUNTIME_VOLUME_ROOT = Path.home() / "volume-agent-gov"
LOCAL_DEBUG_RUNTIME_VOLUME_ROOT = Path("/tmp/local-debug-volume-agent-gov")
RUNTIME_VOLUME_MODES = {"container", "local-debug"}
_RUNTIME_ENV_FILE_MODES = {
    ".env": "container",
    ".env.example": "container",
    ".env.local-debug": "local-debug",
    ".env.local-debug.example": "local-debug",
}
# 仅 governor 需要顶层 claude-roots/<name>；main 已并入业务模型，其 claude-root 在
# data/business-agents/main-agent/claude-root（由 AppSettings 在 get_settings 时创建）。
PROFILE_NAMES = ("governor",)
RUNTIME_DATA_DIRS = (
    "data/sessions",
    "data/transcripts",
    "data/uploads",
    "data/outputs",
    "data/outputs/reports",
    "data/agent-memory",
    "data/feedback-signals",
    "data/soc-events",
    "data/pending-correlations",
    "data/feedback-cases",
    "data/evidence-packages",
    "data/business-agents/main-agent/version/worktrees",
    "data/business-agents/main-agent/version/releases",
    "data/business-agents/main-agent/version/candidate-claude-roots",
    "langfuse/postgres",
    "langfuse/clickhouse/data",
    "langfuse/clickhouse/logs",
    "langfuse/redis",
    "langfuse/minio",
)
SKIP_TEMPLATE_ROOT_FILES = {"README.md", ".template-sanitization.json"}
PRIVATE_RUNTIME_FILENAMES = {".env", ".mcp.local.json", "CLAUDE.local.md", "settings.local.json"}
PRIVATE_RUNTIME_DIR_NAMES = {".git", ".runtime-volume-seeds-backups", "data", "langfuse"}
LEGACY_AGENT_GOVERNANCE_MIGRATIONS = (
    (Path("data/agent-governance/worktrees"), Path("data/business-agents/main-agent/version/worktrees")),
    (Path("data/agent-governance/releases"), Path("data/business-agents/main-agent/version/releases")),
    (
        Path("data/agent-governance/candidate-claude-roots"),
        Path("data/business-agents/main-agent/version/candidate-claude-roots"),
    ),
)


class BootstrapResult(TypedDict):
    created_dirs: list[str]
    copied: list[str]
    skipped_existing: list[str]
    repaired: list[str]
    removed: list[str]
    backups: list[str]
    cleanup_removed: list[str]
    migrated: list[str]
    validation_errors: list[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _expand_env_value(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return env.get(match.group(1), os.environ.get(match.group(1), ""))

    return os.path.expanduser(re.sub(r"\$\{([^}]+)\}", replace, value.strip()))


def _load_env_file(path: Path) -> MutableMapping[str, str]:
    env = dict(os.environ)
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env[key] = _expand_env_value(value, env)
    return env


def _runtime_root_for_mode(mode: str | None) -> Path:
    normalized = (mode or "container").strip()
    if normalized not in RUNTIME_VOLUME_MODES:
        raise ValueError(f"Unsupported runtime volume mode={normalized!r}; expected container or local-debug")
    if normalized == "local-debug":
        return LOCAL_DEBUG_RUNTIME_VOLUME_ROOT
    return CONTAINER_RUNTIME_VOLUME_ROOT


def _runtime_volume_mode_for_env_file(env_file: Path) -> str | None:
    return _RUNTIME_ENV_FILE_MODES.get(env_file.name)


def resolve_runtime_volume_mode(env_file: Path, runtime_root: Path, runtime_volume_mode: str | None = None) -> str:
    if runtime_volume_mode:
        return runtime_volume_mode
    mode = _runtime_volume_mode_for_env_file(env_file)
    if mode:
        return mode
    if runtime_root.resolve() == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT.resolve():
        return "local-debug"
    return "container"


def resolve_runtime_root(cli_value: str | None, env_file: Path, runtime_volume_mode: str | None = None) -> Path:
    if cli_value:
        return Path(os.path.expandvars(os.path.expanduser(cli_value))).resolve()
    env = _load_env_file(env_file)
    value = env.get("HOST_RUNTIME_VOLUME_ROOT")
    if value:
        return Path(value).expanduser().resolve()
    # Legacy compatibility only; official env files derive the mode from their filename.
    mode = runtime_volume_mode or env.get("RUNTIME_VOLUME_MODE") or os.environ.get("RUNTIME_VOLUME_MODE") or _runtime_volume_mode_for_env_file(env_file)
    return _runtime_root_for_mode(mode).resolve()


def _iter_template_entries(template_dir: Path) -> Iterable[Path]:
    for entry in sorted(template_dir.iterdir()):
        if entry.name in SKIP_TEMPLATE_ROOT_FILES:
            continue
        yield entry


def _copy_missing(
    src: Path,
    dest: Path,
    *,
    rel_path: Path,
    overwrite: bool,
    repair_managed_config: bool,
    dry_run: bool,
    render_context: RuntimeTemplateRenderContext,
    copied: list[str],
    skipped: list[str],
    repaired: list[str],
    backups: list[str],
    validation_errors: list[str],
) -> None:
    # #27：data/business-agents/<agent>/ 是活的优化状态（反馈优化闭环 publish 写入的 workspace）——
    # bootstrap 对它只做存在性对账（fill-missing：缺失才补全新 seed Agent），强制关掉 overwrite/repair，
    # 绝不覆盖用户在卷里积累的优化成果与 per-agent git 历史。seed 只是「出生配置」，不回灌已存在 Agent。
    parts = rel_path.parts
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "business-agents":
        overwrite = False
        repair_managed_config = False
    if src.is_dir():
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            _copy_missing(
                child,
                dest / child.name,
                rel_path=rel_path / child.name,
                overwrite=overwrite,
                repair_managed_config=repair_managed_config,
                dry_run=dry_run,
                render_context=render_context,
                copied=copied,
                skipped=skipped,
                repaired=repaired,
                backups=backups,
                validation_errors=validation_errors,
            )
        return
    content: str | None = None
    if is_template_managed_text_file(rel_path):
        content = render_template_file(src.read_text(encoding="utf-8"), rel_path=rel_path, context=render_context)
        validation_errors.extend(validate_rendered_config(content, rel_path=rel_path, context=render_context))
    if dest.exists() and not overwrite:
        if repair_managed_config and content is not None:
            existing = dest.read_text(encoding="utf-8")
            if existing != content:
                repaired.append(dest.as_posix())
                backup = _backup_path(dest, runtime_root=render_context.runtime_root)
                backups.append(backup.as_posix())
                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, backup)
                    dest.write_text(content, encoding="utf-8")
            else:
                skipped.append(dest.as_posix())
            return
        skipped.append(dest.as_posix())
        return
    copied.append(dest.as_posix())
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        shutil.copy2(src, dest)
    else:
        dest.write_text(content, encoding="utf-8")


def _backup_path(path: Path, *, runtime_root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        rel_path = path.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError(f"Refusing to create runtime backup outside runtime root: {path}") from exc
    return runtime_root / ".runtime-volume-seeds-backups" / timestamp / rel_path


def _template_file_set(template_dir: Path) -> set[Path]:
    if not template_dir.exists():
        return set()
    return {path.relative_to(template_dir) for path in template_dir.rglob("*") if path.is_file()}


def _is_stale_template_doc_candidate(rel_path: Path, template_files: set[Path]) -> bool:
    if rel_path in template_files:
        return False
    parts = rel_path.parts
    if len(parts) < 2:
        return False
    if parts[0] not in _workspace_dir_names():
        return False
    if any(part in PRIVATE_RUNTIME_DIR_NAMES for part in parts):
        return False
    if rel_path.name in PRIVATE_RUNTIME_FILENAMES or ".bak-" in rel_path.name:
        return False
    if rel_path.suffix != ".md":
        return False
    if len(parts) == 2 and rel_path.name == "README.md":
        return True
    if len(parts) >= 3 and parts[1] == "docs":
        return True
    return len(parts) >= 3 and parts[1] in {"hooks", "mcp_servers"} and rel_path.name == "README.md"


def _workspace_dir_names() -> set[str]:
    # 顶层挂载的 workspace 种子目录名；main 已迁入 data/business-agents/main-agent/workspace，
    # 不再是顶层 workspace（其配置作为预制业务 Agent 种子随 data/ 子树拷入卷）。
    return {
        "governor-workspace",
    }


def _remove_empty_parents(path: Path, *, stop_at: Path) -> None:
    current = path
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _migrate_legacy_agent_governance_dirs(*, runtime_root: Path, dry_run: bool, migrated: list[str]) -> None:
    for legacy_rel, target_rel in LEGACY_AGENT_GOVERNANCE_MIGRATIONS:
        legacy = runtime_root / legacy_rel
        target = runtime_root / target_rel
        if not legacy.is_dir():
            continue
        if not target.exists():
            migrated.append(f"{legacy.as_posix()} -> {target.as_posix()}")
            if dry_run:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(legacy.as_posix(), target.as_posix())
            _remove_empty_parents(legacy.parent, stop_at=runtime_root / "data")
            continue
        _merge_legacy_dir(legacy=legacy, target=target, runtime_root=runtime_root, dry_run=dry_run, migrated=migrated)


def _merge_legacy_dir(
    *,
    legacy: Path,
    target: Path,
    runtime_root: Path,
    dry_run: bool,
    migrated: list[str],
) -> None:
    children = sorted(legacy.iterdir())
    if dry_run:
        migrated.extend(f"{child.as_posix()} -> {(target / child.name).as_posix()}" for child in children if not (target / child.name).exists())
        return
    target.mkdir(parents=True, exist_ok=True)
    for child in children:
        destination = target / child.name
        if destination.exists():
            continue
        migrated.append(f"{child.as_posix()} -> {destination.as_posix()}")
        shutil.move(child.as_posix(), destination.as_posix())
    try:
        legacy.rmdir()
    except OSError:
        return
    _remove_empty_parents(legacy.parent, stop_at=runtime_root / "data")


def _remove_stale_template_docs(
    *,
    runtime_root: Path,
    template_dir: Path,
    dry_run: bool,
    removed: list[str],
    backups: list[str],
) -> None:
    template_files = _template_file_set(template_dir)
    if not runtime_root.exists():
        return
    for path in sorted(runtime_root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(runtime_root)
        if not _is_stale_template_doc_candidate(rel_path, template_files):
            continue
        removed.append(path.as_posix())
        backup = _backup_path(path, runtime_root=runtime_root)
        backups.append(backup.as_posix())
        if dry_run:
            continue
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.unlink()
        _remove_empty_parents(path.parent, stop_at=runtime_root / rel_path.parts[0])


def bootstrap_runtime_volume(
    *,
    runtime_root: Path,
    template_dir: Path,
    runtime_volume_mode: str = "container",
    env: MutableMapping[str, str] | None = None,
    overwrite: bool = False,
    repair_managed_config: bool = False,
    dry_run: bool = False,
) -> BootstrapResult:
    copied: list[str] = []
    skipped: list[str] = []
    repaired: list[str] = []
    removed: list[str] = []
    backups: list[str] = []
    cleanup_removed: list[str] = []
    migrated: list[str] = []
    validation_errors: list[str] = []
    created_dirs: list[str] = []
    render_context = build_render_context(
        mode=runtime_volume_mode,
        env=env or {},
        runtime_root=runtime_root,
    )

    for rel in RUNTIME_DATA_DIRS:
        path = runtime_root / rel
        created_dirs.append(path.as_posix())
        if not dry_run:
            path.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_agent_governance_dirs(runtime_root=runtime_root, dry_run=dry_run, migrated=migrated)
    for profile in PROFILE_NAMES:
        path = runtime_root / "claude-roots" / profile / ".claude"
        created_dirs.append(path.as_posix())
        if not dry_run:
            path.mkdir(parents=True, exist_ok=True)

    if template_dir.exists():
        for entry in _iter_template_entries(template_dir):
            _copy_missing(
                entry,
                runtime_root / entry.name,
                rel_path=Path(entry.name),
                overwrite=overwrite,
                repair_managed_config=repair_managed_config,
                dry_run=dry_run,
                render_context=render_context,
                copied=copied,
                skipped=skipped,
                repaired=repaired,
                backups=backups,
                validation_errors=validation_errors,
            )
        if repair_managed_config:
            _remove_stale_template_docs(
                runtime_root=runtime_root,
                template_dir=template_dir,
                dry_run=dry_run,
                removed=removed,
                backups=backups,
            )
            if not validation_errors:
                cleanup_result = cleanup_runtime_artifacts(runtime_root=runtime_root, dry_run=dry_run)
                cleanup_removed.extend(cleanup_result["removed"])

    return {
        "created_dirs": created_dirs,
        "copied": copied,
        "skipped_existing": skipped,
        "repaired": repaired,
        "removed": removed,
        "backups": backups,
        "cleanup_removed": cleanup_removed,
        "migrated": migrated,
        "validation_errors": validation_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap runtime volume from docker/runtime-volume-seeds.")
    parser.add_argument("--runtime-root", help="Host runtime root. Defaults to HOST_RUNTIME_VOLUME_ROOT or the selected runtime volume mode.")
    parser.add_argument(
        "--runtime-volume-mode",
        choices=sorted(RUNTIME_VOLUME_MODES),
        help="Default runtime root mode when HOST_RUNTIME_VOLUME_ROOT is not set: container=~/volume-agent-gov, local-debug=/tmp/local-debug-volume-agent-gov.",
    )
    parser.add_argument("--template-dir", type=Path, default=_repo_root() / DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--env-file", type=Path, default=_repo_root() / DEFAULT_ENV_FILE)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing runtime files. Default is fill-missing only.")
    parser.add_argument(
        "--repair-managed-config",
        action="store_true",
        help="Re-render existing runtime-volume-seeds managed text files; remove transient backups and stale template README/docs files after successful validation.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file, args.runtime_volume_mode)
    runtime_volume_mode = resolve_runtime_volume_mode(args.env_file, runtime_root, args.runtime_volume_mode)
    env = _load_env_file(args.env_file)
    template_dir = args.template_dir.resolve()
    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=template_dir,
        runtime_volume_mode=runtime_volume_mode,
        env=env,
        overwrite=args.overwrite,
        repair_managed_config=args.repair_managed_config,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(
            json.dumps(
                {
                    "runtime_root": runtime_root.as_posix(),
                    "runtime_volume_mode": runtime_volume_mode,
                    "template_dir": template_dir.as_posix(),
                    **result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
