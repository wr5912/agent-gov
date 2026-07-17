"""Seed 目录解析的单一真相源。

两层各司其职，不要混用：

- **仓库出生配置**（`runtime_volume_seeds_dir()`）：`docker/runtime-volume-seeds/`，容器内只读
  挂载。它随代码版本发布、可审计、可复现，是「换一个空运行卷时平台自带什么」的答案。
- **运行态 seed catalog**（`runtime_seed_catalog_dir(data_dir)`）：运行卷内可读写副本，由
  bootstrap 从仓库出生配置填充。它是「当前这套运行态里存在哪些 seed」的答案，可被在线归档
  写入、被在线删除移除。

业务 Agent 的出生与 origin 判定读 catalog，不读仓库——否则在线删除的 seed 会在每次重启复活。
templates 与 governor-workspace 仍直接读仓库：它们不是可被在线管理的对象。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from app.runtime.agent_paths import InvalidAgentId, validate_agent_id

_REPO_SEEDS_DIR = Path(__file__).resolve().parents[2] / "docker" / "runtime-volume-seeds"

RUNTIME_SEED_CATALOG_DIRNAME = "seed-catalog"
_DELETION_MARKER_SUFFIX = ".deleted"


def runtime_volume_seeds_dir() -> Path:
    """仓库出生配置根。只读；不受在线删除影响。"""

    explicit = os.environ.get("RUNTIME_VOLUME_SEEDS_DIR")
    return Path(explicit).expanduser().resolve() if explicit else _REPO_SEEDS_DIR


def runtime_seed_catalog_dir(data_dir: Path) -> Path:
    """运行态 seed catalog 根。可读写；业务 Agent 的出生与 origin 以它为准。

    内部沿用与仓库出生配置相同的 `data/business-agents/<id>/workspace` 形状，使
    repo -> catalog 是直接的目录拷贝，不需要格式转换。
    """

    return data_dir / RUNTIME_SEED_CATALOG_DIRNAME


def seed_deletion_marker_path(agent_id: str, *, seed_root: Path) -> Path:
    """已删除 seed 的标记文件路径（与 `<id>/` 同级的 `<id>.deleted`）。

    标记而非「删掉就完事」，是因为仓库出生配置仍在：没有标记时 bootstrap 会在下次启动
    把它重新填充回 catalog，删除就不粘。
    """

    return seed_root / "data" / "business-agents" / f"{agent_id}{_DELETION_MARKER_SUFFIX}"


def is_seed_deleted(agent_id: str, *, seed_root: Path) -> bool:
    return seed_deletion_marker_path(agent_id, seed_root=seed_root).exists()


def business_agent_templates_dir() -> Path:
    """Return the generic business-Agent template catalog."""

    explicit = os.environ.get("BUSINESS_AGENT_TEMPLATES_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return runtime_volume_seeds_dir() / "templates" / "business-agent"


def declared_business_agent_workspace(agent_id: str, *, seed_root: Path | None = None) -> Path:
    root = seed_root or runtime_volume_seeds_dir()
    return root / "data" / "business-agents" / agent_id / "workspace"


def declared_business_agent_ids(*, seed_root: Path | None = None) -> frozenset[str]:
    root = (seed_root or runtime_volume_seeds_dir()) / "data" / "business-agents"
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return frozenset()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return frozenset()

    agent_ids: set[str] = set()
    for child in sorted(root.iterdir()):
        # 先确认 child 本身是真实目录再看 workspace：catalog 里删除标记（`<id>.deleted`）与
        # 条目目录同级，对文件求 `child / "workspace"` 会抛 NotADirectoryError。
        try:
            child_stat = child.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
            continue
        try:
            workspace_stat = (child / "workspace").lstat()
        except OSError:
            continue
        if stat.S_ISLNK(workspace_stat.st_mode) or not stat.S_ISDIR(workspace_stat.st_mode):
            continue
        try:
            agent_ids.add(validate_agent_id(child.name))
        except InvalidAgentId:
            continue
    return frozenset(agent_ids)
