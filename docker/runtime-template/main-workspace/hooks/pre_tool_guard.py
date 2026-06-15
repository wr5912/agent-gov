#!/usr/bin/env python3
"""Claude Code PreToolUse hook: guard high-risk SOC operations.

This hook does not replace real authorization. It adds a deny/allow layer for
obviously risky shell commands and MCP write/execute tools.

运行时为非交互后端（无 permission_prompt_tool_name、permission_mode=default），
SDK 无法呈现 "ask" 交互确认；返回 "ask" 的工具会被无声阻断。因此本 hook 只返回
确定性决策：catastrophic 命令 deny；受限生产 Bash deny；Agent 经对话级确认后需要
执行的 MCP 写入/处置动作 allow（人审在 CLAUDE.md §4 的对话级确认完成，不在 SDK 层）。
"""
import json
import re
import sys

payload = json.load(sys.stdin)
tool_name = payload.get("tool_name", "")
tool_input = payload.get("tool_input", {}) or {}
command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

DENY_PATTERNS = [
    r"rm\s+-rf\s+/(\s|$)",
    r"mkfs\.",
    r"dd\s+if=.*\s+of=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"curl\s+[^|]+\|\s*(sh|bash)",
    r"wget\s+[^|]+\|\s*(sh|bash)",
]
ASK_PATTERNS = [
    r"\biptables\b.*\s-F\b",
    r"\bkubectl\b\s+delete\b",
    r"\bterraform\b\s+apply\b",
    r"\bansible-playbook\b.*(--limit\s+all|production|prod)",
    r"\bsystemctl\b\s+(restart|stop)\b",
    r"\b(nmap|masscan)\b.*(-sS|-sT|-A|--script)",
]

# Block known destructive commands.
for pattern in DENY_PATTERNS:
    if command and re.search(pattern, command, flags=re.IGNORECASE):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "检测到高危破坏性命令，已阻止。请改为生成处置计划或 dry-run。"
            }
        }, ensure_ascii=False))
        sys.exit(0)

# Deny risky production shell commands. Bash 在本运行时按设计受限，且 "ask" 无法在
# 非交互后端呈现确认；这类命令不应由 Agent 直接执行，改为生成处置计划或人工执行。
for pattern in ASK_PATTERNS:
    if command and re.search(pattern, command, flags=re.IGNORECASE):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "该命令可能影响生产环境，已阻止 Agent 直接执行。请改为输出处置计划（含审批、影响范围、回滚方案、验证方法）或由人工执行。"
            }
        }, ensure_ascii=False))
        sys.exit(0)

# Allow MCP write/execute/disposal mutations so the agent can complete an action the
# user has already approved at the conversation level (CLAUDE.md §4). 返回 allow（而非
# 无法满足的 ask），避免用户确认"是"后工具被无声阻断、Agent 反复要求二次确认。
if tool_name.startswith("mcp__"):
    risky_tokens = ("execute", "delete", "update", "write", "block", "isolate", "disable", "kill", "quarantine")
    if any(token in tool_name.lower() for token in risky_tokens):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "MCP 写入/处置动作放行，由对话级确认把关（用户须已在对话中批准）。",
                "additionalContext": "执行前必须已向用户输出处置计划（目标对象、证据、动作、影响范围、回滚方案、验证方法）并获得明确确认；用户已确认则直接执行，不要重复要求确认。"
            }
        }, ensure_ascii=False))
        sys.exit(0)

# No decision means continue with normal permission flow.
sys.exit(0)
