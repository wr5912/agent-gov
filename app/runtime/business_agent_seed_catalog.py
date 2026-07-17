from __future__ import annotations

import os
import stat
from pathlib import Path

from app.runtime.agent_paths import InvalidAgentId, validate_agent_id

_REPO_SEEDS_DIR = Path(__file__).resolve().parents[2] / "docker" / "runtime-volume-seeds"


def runtime_volume_seeds_dir() -> Path:
    """Return the single configured runtime seed catalog root."""

    explicit = os.environ.get("RUNTIME_VOLUME_SEEDS_DIR")
    return Path(explicit).expanduser().resolve() if explicit else _REPO_SEEDS_DIR


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
        workspace = child / "workspace"
        try:
            child_stat = child.lstat()
            workspace_stat = workspace.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(child_stat.st_mode)
            or not stat.S_ISDIR(child_stat.st_mode)
            or stat.S_ISLNK(workspace_stat.st_mode)
            or not stat.S_ISDIR(workspace_stat.st_mode)
        ):
            continue
        try:
            agent_ids.add(validate_agent_id(child.name))
        except InvalidAgentId:
            continue
    return frozenset(agent_ids)
