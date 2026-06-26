"""Issue #1 回归：删除定时任务等高危动作在用户确认后不得陷入二次确认死循环。

根因：运行时为非交互后端（无 permission_prompt_tool_name / permission_mode=default），
SDK 无法呈现 "ask" 交互确认，返回 "ask" 的工具被无声阻断；用户确认"是"后 MCP 删除
被阻断，Agent 反复要求二次确认。整改：pre_tool_guard 对 MCP 写入/处置动作返回 allow，
settings.json 把 MCP 写入/更新/删除放入 allow，CLAUDE.md §4 增加"确认后直接执行、不重复确认"。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_WORKSPACE = ROOT / "docker" / "runtime-volume-seeds" / "main-workspace"
HOOK = MAIN_WORKSPACE / "hooks" / "pre_tool_guard.py"
SETTINGS = MAIN_WORKSPACE / ".claude" / "settings.json"
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


def test_mcp_delete_is_allowed_not_ask_so_confirmed_action_executes():
    """复现 issue #1 核心：delete_snailjob_task 用户确认后必须可执行（allow），不再返回无法满足的 ask。"""
    result = _run_hook({"tool_name": "mcp__snailjob__delete_snailjob_task", "tool_input": {"job_id": 8}})
    assert _decision(result) == "allow"
    # 放行时附带"已确认才执行、不要重复确认"的提醒上下文。
    ctx = result["hookSpecificOutput"].get("additionalContext", "")
    assert "不要重复要求确认" in ctx or "已确认" in ctx


def test_mcp_other_mutations_allowed():
    for tool in ("mcp__sec-ops__block_ip", "mcp__x__update_policy", "mcp__y__write_record", "mcp__z__isolate_host"):
        assert _decision(_run_hook({"tool_name": tool, "tool_input": {}})) == "allow", tool


def test_mcp_query_tool_continues_normal_flow():
    """只读 MCP 查询不被 hook 干预（无决策，走正常权限流）。"""
    assert _run_hook({"tool_name": "mcp__sec-ops-data__query_alerts", "tool_input": {}}) is None


def test_catastrophic_bash_is_denied():
    assert _decision(_run_hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})) == "deny"


def test_risky_production_bash_is_denied_not_ask():
    """受限生产 Bash 改为确定性 deny（ask 在非交互后端无法满足，避免无声阻断）。"""
    for cmd in ("systemctl restart nginx", "kubectl delete pod x", "terraform apply"):
        assert _decision(_run_hook({"tool_name": "Bash", "tool_input": {"command": cmd}})) == "deny", cmd


def test_settings_permissions_have_no_unsatisfiable_ask_on_mcp_mutations():
    """settings.json：MCP 写入/更新/删除必须在 allow，不得留在无法满足的 ask（否则确认后仍被阻断）。"""
    perms = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]
    for rule in ("mcp__*__*write*", "mcp__*__*update*", "mcp__*__*delete*"):
        assert rule in perms["allow"], f"{rule} 应在 allow"
        assert rule not in perms.get("ask", []), f"{rule} 不应在 ask（非交互后端无法确认）"


def test_claude_md_requires_execute_after_confirmation_without_reconfirm():
    """CLAUDE.md §4：用户确认后必须直接执行、禁止重复确认。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "只需确认一次" in text
    assert "禁止再次输出处置计划/确认表格" in text or "禁止重复追问" in text
    assert "立即调用对应工具执行" in text
