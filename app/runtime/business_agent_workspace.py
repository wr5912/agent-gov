from __future__ import annotations

import json
from pathlib import Path

_STARTER_CLAUDE_MD = """# {name}

本工作区是 AgentGov 注册的业务 Agent `{agent_id}`（被治理对象）。

在此定义该 Agent 的角色、system prompt、技能与工具边界、行为约束；AgentGov 负责
其运行、反馈归因、评估和版本治理。高风险动作须经外部系统或授权用户确认。
"""

# 业务 Agent 是被治理对象：起始权限保守，默认只读自身工作区，写/执行需确认，
# 并拒绝读取本地 env、密钥目录，避免配置容器成为凭据泄露面。运行时治理根隔离由
# build_business_agent_profile 在 profile 层另行拒绝。
_STARTER_SETTINGS: dict = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {
        "defaultMode": "default",
        "disableBypassPermissionsMode": "disable",
        "allow": ["Read(./**)", "Glob", "Grep", "Skill"],
        "ask": ["Bash(*)", "Edit(./**)", "Write(./**)"],
        "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"],
    },
}

# 起始 MCP 配置为空：不预置任何 server，更不预置 header/凭据；由用户按需添加。
_STARTER_MCP: dict = {"mcpServers": {}}


def _write_if_absent(path: Path, content: str) -> None:
    """仅在文件不存在时写入，保留用户对该业务 Agent 配置的编辑。"""
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def initialize_business_agent_workspace(workspace_dir: Path, *, agent_id: str, name: str) -> None:
    """幂等初始化业务 Agent 工作区配置容器。

    建立 workspace 与 .claude 目录，并写入 SDK 原生配置文件——CLAUDE.md（system prompt）、
    .claude/settings.json（技能/工具/权限边界）、.mcp.json（MCP 边界）。运行该业务 Agent 时
    （cwd=workspace）这些文件被 Claude SDK 真实加载，构成其可编辑的配置面。

    所有文件已存在则不覆盖（保留用户编辑）；起始模板不含任何 API key、MCP header 或
    本机私有路径。FS 副作用幂等，可安全重复调用。
    """
    claude_dir = workspace_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    _write_if_absent(workspace_dir / "CLAUDE.md", _STARTER_CLAUDE_MD.format(name=name, agent_id=agent_id))
    _write_if_absent(claude_dir / "settings.json", json.dumps(_STARTER_SETTINGS, ensure_ascii=False, indent=2) + "\n")
    _write_if_absent(workspace_dir / ".mcp.json", json.dumps(_STARTER_MCP, ensure_ascii=False, indent=2) + "\n")
