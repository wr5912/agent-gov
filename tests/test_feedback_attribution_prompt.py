from __future__ import annotations

from app.runtime.prompts.feedback_prompts import attribution_prompt


def test_attribution_prompt_scopes_placeholder_attribution_to_effective_mcp_config() -> None:
    prompt = attribution_prompt()

    assert "只有 effective_mcp_config.json 显示选中的 MCP config 或 MCP config path" in prompt
    assert ".claude/settings.json 若影响权限、sandbox 或网络域名" in prompt
    assert "mcp_servers/**/sample*.json 若作为 MCP 工具返回数据污染回答" in prompt
    assert "README、docs、*.example 只作为说明或示例" in prompt
    assert "*.sh 中的 ${VAR:-default} 通常是 shell 默认值语法" in prompt
