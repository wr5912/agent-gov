"""业务 Agent 在运行卷下的目录布局单一真相来源。

此前 ``data_dir/"business-agents"/<agent_id>`` 这一布局以字符串字面量散落在
``routers/agents.py``、``runtime/agent_profiles.py``、``services/agent_governance.py``
三处（architecture.md 禁止的"同一职责跨 3+ 文件字面量耦合"）。这里收敛为单一 helper：
任何创建/解析业务 Agent workspace、claude-root、版本库的代码都从此处取路径，改布局只改一处。

约定（每个注册业务 Agent，含 main-agent），三者**并列**于 ``<id>/`` 下：
- ``workspace``     配置层（CLAUDE.md/.claude/.mcp.json）= cwd + git 版本源（repository_dir）
- ``claude_root``   SDK 运行态家目录（CLAUDE_CONFIG_DIR 的家）；与 workspace 并列，天然不进版本源
- ``version_base``  per-agent 版本治理工件根（其下 worktrees/releases；repo 即 workspace 本身）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BUSINESS_AGENTS_DIRNAME = "business-agents"

# agent_id 直接作为 data_dir 下的路径段，必须防目录穿越/分隔符注入。单一真相：
# 创建（agents.py）、版本治理（_store_for）、路径解析（business_agent_layout）全链路复用。
_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class InvalidAgentId(ValueError):
    """agent_id 不安全（空、含路径分隔符/穿越、非法字符）。"""


def validate_agent_id(agent_id: str | None) -> str:
    """校验并返回安全 agent_id；非法抛 InvalidAgentId（路由层投影为 422/400）。"""
    normalized = (agent_id or "").strip()
    if not normalized or normalized in {".", ".."} or not _SAFE_AGENT_ID.match(normalized):
        raise InvalidAgentId(f"Invalid agent_id: {agent_id!r}")
    return normalized


@dataclass(frozen=True)
class BusinessAgentLayout:
    """单个业务 Agent 在运行卷下的目录布局。"""

    root: Path
    workspace: Path
    claude_root: Path
    version_base: Path


def business_agents_root(data_dir: Path) -> Path:
    """所有业务 Agent 的容器目录（不是单个 Agent 的修改目标）。"""
    return data_dir / BUSINESS_AGENTS_DIRNAME


def business_agent_layout(data_dir: Path, agent_id: str) -> BusinessAgentLayout:
    """解析单个业务 Agent 的运行卷目录布局（先校验 agent_id 防目录穿越）。"""
    safe_id = validate_agent_id(agent_id)
    root = business_agents_root(data_dir) / safe_id
    return BusinessAgentLayout(
        root=root,
        workspace=root / "workspace",
        claude_root=root / "claude-root",
        version_base=root / "version",
    )
