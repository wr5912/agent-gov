"""运行卷初始化源路径解析。

初始化源只回答“空运行卷随产品提供什么”。它不是模板 catalog，也不是运行态副本；业务 Agent
创建统一走 Workspace 包导入，已有运行态 Workspace 不从这里回灌。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.runtime.protected_business_agents import BUILTIN_BUSINESS_AGENT_IDS

_REPO_RUNTIME_BOOTSTRAP_DIR = Path(__file__).resolve().parents[2] / "docker" / "runtime-bootstrap"


def runtime_bootstrap_dir() -> Path:
    explicit = os.environ.get("RUNTIME_BOOTSTRAP_DIR")
    return Path(explicit).expanduser().resolve() if explicit else _REPO_RUNTIME_BOOTSTRAP_DIR


def builtin_business_agent_workspace(agent_id: str, *, bootstrap_root: Path | None = None) -> Path:
    if agent_id not in BUILTIN_BUSINESS_AGENT_IDS:
        raise ValueError(f"Business Agent is not built in: {agent_id}")
    root = bootstrap_root or runtime_bootstrap_dir()
    return root / "business-agents" / agent_id / "workspace"
