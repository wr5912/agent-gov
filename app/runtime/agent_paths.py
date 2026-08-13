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
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

BUSINESS_AGENTS_DIRNAME = "business-agents"
BUSINESS_AGENT_REPOSITORY_LOCKS_DIRNAME = ".agent-repository-locks"

# agent_id 直接作为 data_dir 下的路径段，必须防目录穿越/分隔符注入。单一真相：
# 创建、OpenAPI path schema、版本治理与路径解析全链路复用同一字符集和长度上限。
AGENT_ID_MAX_LENGTH = 128
AGENT_ID_PATTERN = rf"^(?:[A-Za-z0-9_-]|(?:[A-Za-z0-9_-][A-Za-z0-9._-]|\.[A-Za-z0-9_-])|[A-Za-z0-9._-]{{3,{AGENT_ID_MAX_LENGTH}}})$"
_SAFE_AGENT_ID = re.compile(AGENT_ID_PATTERN)


class InvalidAgentId(ValueError):
    """agent_id 不安全（空、含路径分隔符/穿越、非法字符）。"""


def validate_agent_id(agent_id: str | None) -> str:
    """校验并返回安全 agent_id；非法抛 InvalidAgentId（路由层投影为 422/400）。"""
    normalized = (agent_id or "").strip()
    if not normalized or _SAFE_AGENT_ID.fullmatch(normalized) is None:
        raise InvalidAgentId(f"Invalid agent_id: {agent_id!r}")
    return normalized


AgentId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=AGENT_ID_MAX_LENGTH,
        pattern=AGENT_ID_PATTERN,
    ),
    AfterValidator(validate_agent_id),
]


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


def business_agent_repository_lock_path(data_dir: Path, agent_id: str) -> Path:
    """返回不会随 Agent layout 删除而消失的仓库写锁 authority。"""

    safe_id = validate_agent_id(agent_id)
    return data_dir / BUSINESS_AGENT_REPOSITORY_LOCKS_DIRNAME / f"{safe_id}.lock"


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
