"""业务 Agent 创建模板 catalog 与 seeding。

覆盖：catalog 列举、占位渲染、幂等、未知 template_id 越权输入拒绝。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.runtime.business_agent_workspace import (
    DEFAULT_TEMPLATE_ID,
    UnknownBusinessAgentTemplate,
    list_business_agent_templates,
    seed_business_agent_workspace,
)


def test_general_template_present_in_catalog() -> None:
    assert DEFAULT_TEMPLATE_ID in list_business_agent_templates()


def test_seed_renders_placeholders_and_is_idempotent(tmp_path: Path) -> None:
    ws = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    used = seed_business_agent_workspace(ws, agent_id="soc-ops", name="SOC 助手", template_id="general")
    assert used == "general"

    claude_md = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert "soc-ops" in claude_md and "SOC 助手" in claude_md
    # 占位符必须被渲染干净，不留 {{...}}。
    assert "{{AGENT_ID}}" not in claude_md and "{{AGENT_NAME}}" not in claude_md
    assert (ws / ".claude" / "settings.json").exists()
    assert (ws / ".mcp.json").exists()
    permissions = json.loads((ws / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]
    assert "Bash(*)" in permissions["allow"]
    assert "Bash(*)" not in permissions["ask"]
    # catalog 的 README 不应被播种进 workspace。
    assert not (ws / "README.md").exists()

    # 幂等：再次播种不覆盖用户已有编辑。
    (ws / "CLAUDE.md").write_text("EDITED", encoding="utf-8")
    seed_business_agent_workspace(ws, agent_id="soc-ops", name="SOC 助手", template_id="general")
    assert (ws / "CLAUDE.md").read_text(encoding="utf-8") == "EDITED"


def test_unknown_template_id_rejected(tmp_path: Path) -> None:
    """外部输入：未知 template_id 必须拒绝（路由层投影为 422），不静默回退。"""
    with pytest.raises(UnknownBusinessAgentTemplate):
        seed_business_agent_workspace(
            tmp_path / "ws", agent_id="x", name="X", template_id="does-not-exist-../escape"
        )
