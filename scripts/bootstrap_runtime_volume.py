#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import TypedDict

# 本脚本必须可作为独立脚本运行：Dockerfile 只 COPY 它本身（不带 app 包），runtime-init 与
# Makefile 都直接执行它。因此这里不 import app.*，与 catalog 布局相关的常量在下方内联，并由
# tests/test_seed_catalog_bootstrap.py 断言两侧一致。
RUNTIME_SEED_CATALOG_DIRNAME = "seed-catalog"
SEED_DELETION_MARKER_SUFFIX = ".deleted"
# 受保护 Agent：配置与 seed 在仓库维护，bootstrap 强制确保其 catalog 条目存在。
PROTECTED_BUSINESS_AGENT_IDS = frozenset({"security-operations-expert"})

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
    # main-agent 的 version/claude-root 目录不在此无条件创建：main 是可删除的普通业务 Agent，
    # 固定目录会在它被删除后每次启动重建骨架，使删除不粘。这些目录由通用机制按需供给
    # （workspace 经 seed catalog 播种，version 由 GitAgentVersionStore.ensure_bootstrap 建）。
    "langfuse/postgres",
    "langfuse/clickhouse/data",
    "langfuse/clickhouse/logs",
    "langfuse/redis",
    "langfuse/minio",
)
SKIP_TEMPLATE_ROOT_ENTRIES = {"README.md", ".template-sanitization.json", "workspace-policy"}
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
    seed_catalog_copied: list[str]
    seed_catalog_skipped_deleted: list[str]


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


def _iter_template_entries(template_dir: Path) -> Iterable[Path]:
    for entry in sorted(template_dir.iterdir()):
        if entry.name in SKIP_TEMPLATE_ROOT_ENTRIES:
            continue
        # data/business-agents 不从仓库直供 live：它先经运行态 seed catalog（见
        # _sync_seed_catalog），使在线删除的 seed 不会在下次启动被仓库出生配置复活。
        if entry.name == "data":
            continue
        yield entry


def _sync_seed_catalog(
    *,
    runtime_root: Path,
    template_dir: Path,
    dry_run: bool,
    catalog_copied: list[str],
    catalog_skipped_deleted: list[str],
) -> Path:
    """把仓库出生配置同步进运行态 seed catalog，返回 catalog 根。

    三条规则，各自对应一个真实需求：
    - 已被在线删除（存在 `<id>.deleted` 标记）的 seed 跳过——否则删除不粘，重启即复活。
    - catalog 缺失的 seed 整目录复制——新装/换卷时平台自带的内置 Agent 由此而来。
    - 受保护 Agent 强制确保存在并清除标记——它的真相源在仓库，不接受运行态把它删掉。
    """

    catalog_root = runtime_root / "data" / RUNTIME_SEED_CATALOG_DIRNAME
    repo_agents = template_dir / "data" / "business-agents"
    if not repo_agents.is_dir():
        return catalog_root

    catalog_agents = catalog_root / "data" / "business-agents"
    for source in sorted(repo_agents.iterdir()):
        if not source.is_dir() or source.is_symlink():
            continue
        agent_id = source.name
        marker = catalog_agents / f"{agent_id}{SEED_DELETION_MARKER_SUFFIX}"
        protected = agent_id in PROTECTED_BUSINESS_AGENT_IDS
        if protected and marker.exists():
            # 受保护 Agent 不接受运行态删除；标记只可能来自手工投毒或历史数据，直接修复。
            if not dry_run:
                marker.unlink(missing_ok=True)
        elif marker.exists():
            catalog_skipped_deleted.append((catalog_agents / agent_id).as_posix())
            continue
        destination = catalog_agents / agent_id
        if destination.exists() and not protected:
            continue
        # 受保护 Agent 走 fill-missing（补齐缺失文件，不覆盖已有），与 governor-workspace
        # 的「跟随 seed」取向一致，但不抹掉运行态已有内容。
        _copy_missing(
            source,
            destination,
            rel_path=Path("seed-catalog") / agent_id,
            dry_run=dry_run,
            copied=catalog_copied,
            skipped=[],
        )
    return catalog_root


def _seed_live_business_agents(
    *,
    runtime_root: Path,
    catalog_root: Path,
    dry_run: bool,
    copied: list[str],
    skipped: list[str],
) -> None:
    """从运行态 seed catalog 播种 live workspace（整目录 fill-missing）。

    与仓库直供的区别只有源；`_copy_missing` 的「workspace 已存在则整体跳过、绝不逐文件回灌」
    语义原样保留（rel_path 仍是 data/business-agents/<id>/workspace）。
    """

    catalog_agents = catalog_root / "data" / "business-agents"
    if not catalog_agents.is_dir():
        return
    for entry in sorted(catalog_agents.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        workspace = entry / "workspace"
        if not workspace.is_dir() or workspace.is_symlink():
            continue
        rel = Path("data") / "business-agents" / entry.name / "workspace"
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
        raise ValueError(f"Runtime seed entry must be a regular file or directory: {rel_path.as_posix()}")
    # 业务 Agent workspace 是 Git 版本源。只有整个 workspace 不存在时才播种出生配置；
    # 已存在 workspace 不逐文件 fill-missing，避免把版本中有意删除的文件复活。
    parts = rel_path.parts
    is_business_workspace_root = len(parts) == 4 and parts[0] == "data" and parts[1] == "business-agents" and parts[3] == "workspace"
    if is_business_workspace_root and stat.S_ISDIR(source_mode) and dest.exists():
        skipped.append(dest.as_posix())
        return
    # governor-workspace 是平台治理配置（治理执行者的 prompt/权限/skill），不是用户在卷里积累的业务优化态，
    # 应始终跟随 seed：每次 bootstrap 从 seed 强制覆盖，使「改 governor 配置→重建」即在现网生效
    # （只覆盖 seed 中存在的文件，卷内私有/会话文件不动；业务 Agent 卷仍走上面的 fill-missing、绝不回灌）。
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
    template_dir: Path,
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

    catalog_copied: list[str] = []
    catalog_skipped_deleted: list[str] = []
    if template_dir.exists():
        # 一级：仓库出生配置 -> 运行态 seed catalog。
        catalog_root = _sync_seed_catalog(
            runtime_root=runtime_root,
            template_dir=template_dir,
            dry_run=dry_run,
            catalog_copied=catalog_copied,
            catalog_skipped_deleted=catalog_skipped_deleted,
        )
        # 二级：运行态 seed catalog -> live workspace（整目录 fill-missing，语义不变，
        # 只是源从仓库换成 catalog）。已删 seed 不在 catalog，因此不再产生 live 孤儿目录。
        _seed_live_business_agents(
            runtime_root=runtime_root,
            catalog_root=catalog_root,
            dry_run=dry_run,
            copied=copied,
            skipped=skipped,
        )
        # 三级：governor-workspace 与 templates 仍直连仓库——它们不是可被在线管理的对象。
        for entry in _iter_template_entries(template_dir):
            _copy_missing(
                entry,
                runtime_root / entry.name,
                rel_path=Path(entry.name),
                dry_run=dry_run,
                copied=copied,
                skipped=skipped,
            )

    return {
        "created_dirs": created_dirs,
        "copied": copied,
        "skipped_existing": skipped,
        "migrated": migrated,
        "seed_catalog_copied": catalog_copied,
        "seed_catalog_skipped_deleted": catalog_skipped_deleted,
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
