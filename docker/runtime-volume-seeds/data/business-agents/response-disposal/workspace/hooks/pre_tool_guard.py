#!/usr/bin/env python3
"""Claude Code PreToolUse hook: hard-deny clearly unsafe operations.

This hook does not replace Claude Code authorization. Web HITL is handled by
Claude Agent SDK can_use_tool; this hook only returns deny for operations that
must never reach human approval.
"""
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    # fail-closed：畸形/空 stdin 时明确 deny，守卫 hook 异常不放行（不 fail-open）。
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "PreToolUse 守卫无法解析工具输入，安全起见已阻止。",
                }
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)
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

# Deny risky production shell commands. Other mutating tools fall through to
# Claude's native ask/can_use_tool path instead of being pseudo-approved here.
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

# No decision means continue with normal permission flow.
sys.exit(0)
