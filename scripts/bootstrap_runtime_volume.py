#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime.protected_business_agents import BUILTIN_BUSINESS_AGENT_IDS  # noqa: E402

DEFAULT_BOOTSTRAP_DIR = Path("docker/runtime-bootstrap")
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
# 仅 governor 需要顶层 claude-roots/<name>；业务 Agent 的 claude-root 在各自运行态目录中。
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
    # 普通业务 Agent 的 version/claude-root 目录不在此无条件创建，由通用机制按需供给。
    "langfuse/postgres",
    "langfuse/clickhouse/data",
    "langfuse/clickhouse/logs",
    "langfuse/redis",
    "langfuse/minio",
)
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
    migrated: list[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _expand_env_value(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return env.get(match.group(1), os.environ.get(match.group(1), ""))

    return os.path.expanduser(re.sub(r"\$\{([^}]+)\}", replace, value.strip()))


def _load_env_file(path: Path) -> MutableMapping[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return dict(os.environ)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env[key] = _expand_env_value(value, env)
    env.update(os.environ)
    return env


def load_runtime_env(path: Path) -> MutableMapping[str, str]:
    """Load the selected runtime env file without mutating process environment."""

    return _load_env_file(path)


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


def _initialize_builtin_business_agents(
    *,
    runtime_root: Path,
    bootstrap_dir: Path,
    dry_run: bool,
    copied: list[str],
    skipped: list[str],
) -> None:
    """只初始化显式内置业务 Agent；整个运行态 Workspace 已存在时绝不回灌。"""

    builtins_root = bootstrap_dir / "business-agents"
    if builtins_root.is_symlink() or not builtins_root.is_dir():
        raise ValueError(f"Runtime bootstrap business-agents root must be a real directory: {builtins_root}")
    actual_ids = {entry.name for entry in builtins_root.iterdir() if entry.is_dir() and not entry.is_symlink()}
    if actual_ids != set(BUILTIN_BUSINESS_AGENT_IDS):
        raise ValueError(
            "Runtime bootstrap built-in business Agents do not match the declared set: "
            f"expected={sorted(BUILTIN_BUSINESS_AGENT_IDS)}, actual={sorted(actual_ids)}"
        )
    for agent_id in sorted(BUILTIN_BUSINESS_AGENT_IDS):
        workspace = builtins_root / agent_id / "workspace"
        if workspace.is_symlink() or not workspace.is_dir() or not any(workspace.iterdir()):
            raise ValueError(f"Built-in business Agent Workspace is missing, unsafe, or empty: {agent_id}")
        rel = Path("data") / "business-agents" / agent_id / "workspace"
        _copy_missing(
            workspace,
            runtime_root / rel,
            rel_path=rel,
            dry_run=dry_run,
            copied=copied,
            skipped=skipped,
        )


def _copy_missing(
    src: Path,
    dest: Path,
    *,
    rel_path: Path,
    dry_run: bool,
    copied: list[str],
    skipped: list[str],
) -> None:
    source_mode = src.lstat().st_mode
    if stat.S_ISLNK(source_mode) or not (stat.S_ISDIR(source_mode) or stat.S_ISREG(source_mode)):
        raise ValueError(f"Runtime bootstrap entry must be a regular file or directory: {rel_path.as_posix()}")
    # 业务 Agent workspace 是 Git 版本源。只有整个 workspace 不存在时才播种出生配置；
    # 已存在 workspace 不逐文件 fill-missing，避免把版本中有意删除的文件复活。
    parts = rel_path.parts
    is_business_workspace_root = len(parts) == 4 and parts[0] == "data" and parts[1] == "business-agents" and parts[3] == "workspace"
    if is_business_workspace_root and stat.S_ISDIR(source_mode) and dest.exists():
        skipped.append(dest.as_posix())
        return
    # governor-workspace 是平台治理配置，不是用户在卷里积累的业务优化态；每次初始化都覆盖
    # 初始化源中存在的文件，但不移除卷内私有文件。业务 Agent Workspace 走上面的整体跳过。
    overwrite = bool(parts and parts[0] == "governor-workspace")
    if stat.S_ISDIR(source_mode):
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            _copy_missing(
                child,
                dest / child.name,
                rel_path=rel_path / child.name,
                dry_run=dry_run,
                copied=copied,
                skipped=skipped,
            )
        return
    if dest.exists() and not overwrite:
        skipped.append(dest.as_posix())
        return
    copied.append(dest.as_posix())
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


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


def bootstrap_runtime_volume(
    *,
    runtime_root: Path,
    bootstrap_dir: Path,
    runtime_volume_mode: str = "container",
    env: MutableMapping[str, str] | None = None,
    dry_run: bool = False,
) -> BootstrapResult:
    del runtime_volume_mode, env
    copied: list[str] = []
    skipped: list[str] = []
    migrated: list[str] = []
    created_dirs: list[str] = []
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

    if bootstrap_dir.is_symlink() or not bootstrap_dir.is_dir():
        raise ValueError(f"Runtime bootstrap source must be a real directory: {bootstrap_dir}")
    governor_workspace = bootstrap_dir / "governor-workspace"
    if governor_workspace.is_symlink() or not governor_workspace.is_dir() or not any(governor_workspace.iterdir()):
        raise ValueError("Runtime bootstrap governor Workspace is missing, unsafe, or empty")
    _copy_missing(
        governor_workspace,
        runtime_root / "governor-workspace",
        rel_path=Path("governor-workspace"),
        dry_run=dry_run,
        copied=copied,
        skipped=skipped,
    )
    _initialize_builtin_business_agents(
        runtime_root=runtime_root,
        bootstrap_dir=bootstrap_dir,
        dry_run=dry_run,
        copied=copied,
        skipped=skipped,
    )

    return {
        "created_dirs": created_dirs,
        "copied": copied,
        "skipped_existing": skipped,
        "migrated": migrated,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap runtime volume from docker/runtime-bootstrap.")
    parser.add_argument("--runtime-root", help="Host runtime root. Defaults to HOST_RUNTIME_VOLUME_ROOT or the selected runtime volume mode.")
    parser.add_argument(
        "--runtime-volume-mode",
        choices=sorted(RUNTIME_VOLUME_MODES),
        help="Default runtime root mode when HOST_RUNTIME_VOLUME_ROOT is not set: container=~/volume-agent-gov, local-debug=/tmp/local-debug-volume-agent-gov.",
    )
    parser.add_argument("--bootstrap-dir", type=Path, default=_repo_root() / DEFAULT_BOOTSTRAP_DIR)
    parser.add_argument("--env-file", type=Path, default=_repo_root() / DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file, args.runtime_volume_mode)
    runtime_volume_mode = resolve_runtime_volume_mode(args.env_file, runtime_root, args.runtime_volume_mode)
    env = _load_env_file(args.env_file)
    bootstrap_dir = args.bootstrap_dir.resolve()
    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        bootstrap_dir=bootstrap_dir,
        runtime_volume_mode=runtime_volume_mode,
        env=env,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(
            json.dumps(
                {
                    "runtime_root": runtime_root.as_posix(),
                    "runtime_volume_mode": runtime_volume_mode,
                    "bootstrap_dir": bootstrap_dir.as_posix(),
                    **result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
