from __future__ import annotations

import json
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]


def _settings() -> dict[str, object]:
    return json.loads((WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_workspace_is_fail_closed_without_shell_or_web_access() -> None:
    policy = _settings()
    permissions = policy["permissions"]
    assert permissions["defaultMode"] == "dontAsk"
    assert permissions["ask"] == []
    assert {"Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"}.issubset(permissions["deny"])
    assert not any(rule.split("(", 1)[0] in {"Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"} for rule in permissions["allow"])


def test_mcp_stays_disabled_until_exact_capability_gate_is_active() -> None:
    permissions = _settings()["permissions"]
    allowed_mcp = {rule for rule in permissions["allow"] if rule.startswith("mcp__")}
    assert allowed_mcp == set()
    assert "mcp__*" in permissions["deny"]
    assert json.loads((WORKSPACE / ".mcp.json").read_text(encoding="utf-8")) == {"mcpServers": {}}


def test_output_writes_are_scoped_to_agent_directory() -> None:
    policy = _settings()
    permissions = policy["permissions"]
    sandbox_fs = policy["sandbox"]["filesystem"]
    expected_output_rules = {
        "Edit(/data/outputs/security-operations-expert/**)",
        "Write(/data/outputs/security-operations-expert/**)",
    }
    assert expected_output_rules.issubset(permissions["allow"])
    assert not any("../" in rule for rule in permissions["allow"] if "outputs/" in rule)
    assert sandbox_fs["allowWrite"] == ["/data/outputs/security-operations-expert"]


def test_pre_tool_hook_only_protects_governance_writes() -> None:
    hooks = _settings()["hooks"]
    assert set(hooks) == {"PreToolUse", "SessionStart"}
    assert [entry["matcher"] for entry in hooks["PreToolUse"]] == ["Edit|Write|NotebookEdit"]
    assert all("Bash" not in entry["matcher"] for entries in hooks.values() for entry in entries)


def test_claude_instructions_define_read_only_defensive_role() -> None:
    text = (WORKSPACE / "CLAUDE.md").read_text(encoding="utf-8")
    assert "仅限已授权环境的防御性安全运营" in text
    assert "Agent 不产生 SOC 副作用" in text
    assert "P0-MCP 回执落地前保持禁用" in text
    assert "/data/outputs/security-operations-expert/**" in text


def test_agent_manifest_declares_identity_and_no_soc_side_effects() -> None:
    text = (WORKSPACE / "agent.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()
    manifest_ids = [line.removeprefix("  id:").strip() for line in lines if line.startswith("  id:")]
    assert manifest_ids == ["security-operations-expert"]
    assert "agent_soc_side_effects: deny" in text
    assert "role: read-only-defensive-advisor" in text
