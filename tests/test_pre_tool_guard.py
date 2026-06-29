"""Claude 原生 Web HITL 回归：MCP mutation 不再由 hook 伪 allow。

高风险 MCP mutation 应落回 Claude Code settings 的 ask 规则，并由 Agent SDK
can_use_tool -> Web 确认卡片完成授权；pre_tool_guard 只保留硬拒绝。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_WORKSPACE = ROOT / "docker" / "runtime-volume-seeds" / "data" / "business-agents" / "main-agent" / "workspace"
RESPONSE_WORKSPACE = ROOT / "docker" / "runtime-volume-seeds" / "data" / "business-agents" / "response-disposal" / "workspace"
HOOK = MAIN_WORKSPACE / "hooks" / "pre_tool_guard.py"
SETTINGS = MAIN_WORKSPACE / ".claude" / "settings.json"
RESPONSE_SETTINGS = RESPONSE_WORKSPACE / ".claude" / "settings.json"
CLAUDE_MD = MAIN_WORKSPACE / "CLAUDE.md"


def _run_hook(payload: dict) -> dict | None:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def _decision(result: dict | None) -> str | None:
    if not result:
        return None
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def test_mcp_delete_continues_to_native_permission_flow():
    """MCP 删除不再 hook allow；由 settings ask -> can_use_tool -> Web 确认处理。"""
    result = _run_hook({"tool_name": "mcp__snailjob__delete_snailjob_task", "tool_input": {"job_id": 8}})
    assert _decision(result) is None


def test_mcp_other_mutations_continue_to_native_permission_flow():
    for tool in ("mcp__sec-ops__block_ip", "mcp__x__update_policy", "mcp__y__write_record", "mcp__z__isolate_host"):
        assert _decision(_run_hook({"tool_name": tool, "tool_input": {}})) is None, tool


def test_mcp_query_tool_continues_normal_flow():
    """只读 MCP 查询不被 hook 干预（无决策，走正常权限流）。"""
    assert _run_hook({"tool_name": "mcp__sec-ops-data__query_alerts", "tool_input": {}}) is None


def test_catastrophic_bash_is_denied():
    assert _decision(_run_hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})) == "deny"


def test_risky_production_bash_is_denied_not_ask():
    """受限生产 Bash 改为确定性 deny（ask 在非交互后端无法满足，避免无声阻断）。"""
    for cmd in ("systemctl restart nginx", "kubectl delete pod x", "terraform apply"):
        assert _decision(_run_hook({"tool_name": "Bash", "tool_input": {"command": cmd}})) == "deny", cmd


def test_settings_permissions_put_mcp_mutations_in_ask_not_allow():
    """settings.json：MCP 写入/更新/删除必须进入 ask，让 Web HITL 接管确认。"""
    perms = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]
    for rule in ("mcp__*__*write*", "mcp__*__*update*", "mcp__*__*delete*"):
        assert rule not in perms["allow"], f"{rule} 不应在 allow"
        assert rule in perms.get("ask", []), f"{rule} 应在 ask"


def test_daily_report_low_risk_permissions_are_direct_allow():
    perms = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]
    sandbox_fs = json.loads(SETTINGS.read_text(encoding="utf-8"))["sandbox"]["filesystem"]

    assert "Write(/data/outputs/**)" in perms["allow"]
    assert "Write(/data/reports/**)" in perms["allow"]
    assert "Bash(date *)" in perms["allow"]
    assert "Bash(pwd)" in perms["allow"]
    assert "Bash(mkdir -p /data/reports/**)" in perms["allow"]
    assert "Bash(*)" in perms.get("ask", [])
    assert "/data/reports" in sandbox_fs["allowWrite"]


def test_response_disposal_execution_mcp_is_ask_not_allow():
    perms = json.loads(RESPONSE_SETTINGS.read_text(encoding="utf-8"))["permissions"]

    assert "mcp__soc-playbook-execution__*" not in perms["allow"]
    assert "mcp__soc-playbook-registry__*" not in perms["allow"]
    assert "mcp__soc-playbook-execution__*" in perms.get("ask", [])
    assert "mcp__soc-playbook-registry__*" in perms.get("ask", [])
    assert "mcp__soc-playbook-execution-result-query__*" in perms["allow"]


def test_claude_md_requires_execute_after_confirmation_without_reconfirm():
    """CLAUDE.md §4：对话计划与 Claude 原生工具确认分层，禁止重复确认。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "Claude 原生 Web 确认卡片" in text
    assert "不要重复输出处置计划/确认表格" in text
    assert "不要把普通对话回复当成绕过工具权限的依据" in text
