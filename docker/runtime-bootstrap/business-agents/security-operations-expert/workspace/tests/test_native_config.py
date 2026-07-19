from __future__ import annotations

import json
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]


def test_supported_submission_tools_use_native_ask_rules() -> None:
    policy = json.loads((WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = policy["permissions"]
    assert set(permissions["ask"]) == {
        "mcp__sec-ops__soc_api__create",
        "mcp__sec-ops__soc_api__manual",
    }
    assert "mcp__sec-ops__soc_api__execute" in permissions["deny"]
    assert "mcp__sec-ops__soc_api__update*" in permissions["deny"]
    assert "mcp__sec-ops__soc_api__delete*" in permissions["deny"]


def test_workspace_has_explicit_output_and_bash_permissions() -> None:
    policy = json.loads((WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = policy["permissions"]
    sandbox_fs = policy["sandbox"]["filesystem"]
    assert "Write(../../../outputs/security-operations-expert/**)" in permissions["allow"]
    assert "Bash(*)" in permissions["allow"]
    assert "../../../outputs/security-operations-expert" in sandbox_fs["allowWrite"]


def test_claude_instructions_keep_each_tool_card_as_confirmation_object() -> None:
    text = (WORKSPACE / "CLAUDE.md").read_text(encoding="utf-8")
    assert "工具卡输入就是确认对象" in text
    assert "不得在确认后修改工具参数" in text
    assert "不得请求 run 级放行" in text


def test_agent_manifest_does_not_declare_registry_identity() -> None:
    text = (WORKSPACE / "agent.yaml").read_text(encoding="utf-8")
    assert "\n  id:" not in text
