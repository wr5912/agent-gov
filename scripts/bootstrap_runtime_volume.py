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
    from scripts.runtime_template_renderer import (
        RuntimeTemplateRenderContext,
        build_render_context,
        is_template_managed_text_file,
        render_template_file,
        validate_rendered_config,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from runtime_template_renderer import (
        RuntimeTemplateRenderContext,
        build_render_context,
        is_template_managed_text_file,
        render_template_file,
        validate_rendered_config,
    )

DEFAULT_TEMPLATE_DIR = Path("docker/runtime-template")
DEFAULT_ENV_FILE = Path("docker/.env")
CONTAINER_RUNTIME_VOLUME_ROOT = Path.home() / "volume-agent-runtime"
LOCAL_DEBUG_RUNTIME_VOLUME_ROOT = Path("/tmp/local-debug-volume-agent-runtime")
RUNTIME_VOLUME_MODES = {"container", "local-debug"}
_RUNTIME_ENV_FILE_MODES = {
    ".env": "container",
    ".env.example": "container",
    ".env.local-debug": "local-debug",
    ".env.local-debug.example": "local-debug",
}
PROFILE_NAMES = (
    "main",
    "attribution-analyzer",
    "proposal-generator",
    "execution-optimizer",
    "eval-case-governor",
    "regression-impact-analyzer",
)
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
    "data/feedback-analysis/jobs",
    "data/optimization-proposals",
    "data/optimization-tasks",
    "data/agent-governance/worktrees",
    "data/agent-governance/releases",
    "langfuse/postgres",
    "langfuse/clickhouse/data",
    "langfuse/clickhouse/logs",
    "langfuse/redis",
    "langfuse/minio",
)
SKIP_TEMPLATE_ROOT_FILES = {"README.md", ".template-sanitization.json"}
PRIVATE_RUNTIME_FILENAMES = {".env", ".mcp.local.json", "CLAUDE.local.md", "settings.local.json"}
PRIVATE_RUNTIME_DIR_NAMES = {".git", ".runtime-template-backups", "data", "langfuse"}


class BootstrapResult(TypedDict):
    created_dirs: list[str]
    copied: list[str]
    skipped_existing: list[str]
    repaired: list[str]
    removed: list[str]
    backups: list[str]
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
    except ValueError:
        return path.with_name(f"{path.name}.bak-{timestamp}")
    return runtime_root / ".runtime-template-backups" / timestamp / rel_path


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
    return {
        "main-workspace",
        "attribution-analyzer-workspace",
        "proposal-generator-workspace",
        "execution-optimizer-workspace",
        "eval-case-governor-workspace",
        "regression-impact-analyzer-workspace",
    }


def _remove_empty_parents(path: Path, *, stop_at: Path) -> None:
    current = path
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


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

    return {
        "created_dirs": created_dirs,
        "copied": copied,
        "skipped_existing": skipped,
        "repaired": repaired,
        "removed": removed,
        "backups": backups,
        "validation_errors": validation_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap runtime volume from docker/runtime-template.")
    parser.add_argument("--runtime-root", help="Host runtime root. Defaults to HOST_RUNTIME_VOLUME_ROOT or the selected runtime volume mode.")
    parser.add_argument(
        "--runtime-volume-mode",
        choices=sorted(RUNTIME_VOLUME_MODES),
        help="Default runtime root mode when HOST_RUNTIME_VOLUME_ROOT is not set: container=~/volume-agent-runtime, local-debug=/tmp/local-debug-volume-agent-runtime.",
    )
    parser.add_argument("--template-dir", type=Path, default=_repo_root() / DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--env-file", type=Path, default=_repo_root() / DEFAULT_ENV_FILE)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing runtime files. Default is fill-missing only.")
    parser.add_argument(
        "--repair-managed-config",
        action="store_true",
        help="Re-render existing runtime-template managed text files after backing them up; also remove stale template README/docs files.",
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
