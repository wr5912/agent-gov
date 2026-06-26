"""业务 Agent 在运行卷下的目录布局单一真相来源。

此前 ``data_dir/"business-agents"/<agent_id>`` 这一布局以字符串字面量散落在
``routers/agents.py``、``runtime/agent_profiles.py``、``services/agent_governance.py``
三处（architecture.md 禁止的"同一职责跨 3+ 文件字面量耦合"）。这里收敛为单一 helper：
任何创建/解析业务 Agent workspace、claude-root、版本库的代码都从此处取路径，改布局只改一处。

约定（每个业务 Agent，含预制 main-agent）：
- ``workspace``     配置层（CLAUDE.md/.claude/.mcp.json）= cwd + git 版本源
- ``claude_root``   SDK 运行态家目录（CLAUDE_CONFIG_DIR 的家）
- ``version_base``  per-agent git 版本库根（其下 repo/worktrees/releases）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BUSINESS_AGENTS_DIRNAME = "business-agents"


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
    """解析单个业务 Agent 的运行卷目录布局。"""
    root = business_agents_root(data_dir) / agent_id
    return BusinessAgentLayout(
        root=root,
        workspace=root,
        claude_root=root / "claude-root",
        version_base=root / "version",
    )
