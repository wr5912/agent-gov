#!/usr/bin/env python3
"""Claude Code PreToolUse hook: guard high-risk SOC operations.

This hook does not replace real authorization. It adds an extra prompt/deny layer for
obviously risky shell commands and MCP write/execute tools.
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

# Ask on risky shell commands.
for pattern in ASK_PATTERNS:
    if command and re.search(pattern, command, flags=re.IGNORECASE):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "该命令可能影响生产环境或造成较大扫描/变更风险，请确认用途、范围和回滚方案。",
                "additionalContext": "安全运营规范：生产变更必须先 dry-run，并包含审批、影响范围、回滚方案和验证方法。"
            }
        }, ensure_ascii=False))
        sys.exit(0)

# Ask on response execution and generic MCP mutations.
if tool_name.startswith("mcp__"):
    risky_tokens = ("execute", "delete", "update", "write", "block", "isolate", "disable", "kill", "quarantine")
    if any(token in tool_name.lower() for token in risky_tokens):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "检测到 MCP 可能执行写入/处置/策略变更动作，需要用户明确授权。",
                "additionalContext": "请确认：目标对象、证据、动作、影响范围、回滚方案、验证方法。"
            }
        }, ensure_ascii=False))
        sys.exit(0)

# No decision means continue with normal permission flow.
sys.exit(0)
