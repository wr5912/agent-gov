from __future__ import annotations

from pathlib import Path

_STARTER_CLAUDE_MD = """# {name}

本工作区是 AgentGov 注册的业务 Agent `{agent_id}`（被治理对象）。

在此定义该 Agent 的角色、system prompt、技能与工具边界、行为约束；AgentGov 负责
其运行、反馈归因、评估和版本治理。高风险动作须经外部系统或授权用户确认。
"""


def initialize_business_agent_workspace(workspace_dir: Path, *, agent_id: str, name: str) -> None:
    """幂等初始化业务 Agent 工作区。

    建立 workspace 与 .claude 目录，并写入起始 CLAUDE.md（已存在则不覆盖，避免
    冲掉用户对该业务 Agent 行为配置的编辑）。FS 副作用幂等，可安全重复调用。
    """
    (workspace_dir / ".claude").mkdir(parents=True, exist_ok=True)
    claude_md = workspace_dir / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(_STARTER_CLAUDE_MD.format(name=name, agent_id=agent_id), encoding="utf-8")
