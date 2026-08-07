from __future__ import annotations

import json
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]


def test_soc_write_tools_are_denied_without_native_ask_rules() -> None:
    policy = json.loads((WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = policy["permissions"]
    assert permissions["ask"] == []
    assert {
        "mcp__sec-ops__soc_api__create*",
        "mcp__sec-ops__soc_api__manual",
        "mcp__sec-ops__soc_api__execute",
        "mcp__sec-ops__soc_api__post_*",
        "mcp__sec-ops__soc_api__put_*",
        "mcp__sec-ops__soc_api__delete_*",
        "mcp__sec-ops__soc_api__patch_*",
        "mcp__sec-ops__soc_api__update*",
        "mcp__sec-ops__soc_api__delete*",
        "mcp__sec-ops__soc_api__upload*",
        "mcp__sec-ops__soc_api__cancel*",
        "mcp__sec-ops__soc_api__rollback",
    }.issubset(permissions["deny"])


def test_workspace_has_explicit_output_and_bash_permissions() -> None:
    policy = json.loads((WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = policy["permissions"]
    sandbox_fs = policy["sandbox"]["filesystem"]
    bash_rules = {rule for rule in permissions["allow"] if rule.startswith("Bash(")}
    assert bash_rules == {
        "Bash(jq *)",
        "Bash(date *)",
        "Bash(date)",
        "Bash(pwd)",
        "Bash(mkdir -p /data/outputs/security-operations-expert/**)",
    }
    assert "Bash(*)" not in permissions["allow"]
    assert "Read(/data/outputs/security-operations-expert/**)" in permissions["allow"]
    assert "Edit(/data/outputs/security-operations-expert/**)" in permissions["allow"]
    assert "Write(/data/outputs/security-operations-expert/**)" in permissions["allow"]
    assert not any("../" in rule for rule in permissions["allow"] if "outputs/" in rule)
    assert sandbox_fs["allowWrite"] == ["/data/outputs/security-operations-expert"]


def test_claude_instructions_keep_agent_read_only_and_ro_as_lifecycle_owner() -> None:
    text = (WORKSPACE / "CLAUDE.md").read_text(encoding="utf-8")
    assert "本 Agent 在整个在线流程中始终是只读候选提供者" in text
    assert "Agent 只负责筛选、生成或修订完整剧本" in text
    assert "保存、启停、删除、SOC manual 执行（内含预检）和实例监控全部由响应处置 lifecycle worker 完成" in text
    assert "RO lifecycle worker" in text
    assert "Agent 不持有该 token" in text


def test_agent_manifest_declares_expected_identity() -> None:
    lines = (WORKSPACE / "agent.yaml").read_text(encoding="utf-8").splitlines()
    manifest_ids = [line.removeprefix("  id:").strip() for line in lines if line.startswith("  id:")]
    assert manifest_ids == ["security-operations-expert"]
