#!/usr/bin/env python3
"""Claude Code PreToolUse hook: hard-deny unsafe or mutating operations."""

import json
import re
import sys

payload = json.load(sys.stdin)
tool_name = payload.get("tool_name", "")
tool_input = payload.get("tool_input", {}) or {}
command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

DENY_BASH_PATTERNS = [
    r"rm\s+-rf\s+/(\s|$)",
    r"mkfs\.",
    r"dd\s+if=.*\s+of=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"curl\s+[^|]+\|\s*(sh|bash)",
    r"wget\s+[^|]+\|\s*(sh|bash)",
    r"\bkubectl\b\s+(delete|apply|scale|rollout)\b",
    r"\bterraform\b\s+apply\b",
    r"\bsystemctl\b\s+(restart|stop)\b",
]
DENY_MCP_NAME = re.compile(
    r"(write|update|delete|block|isolate|disable|kill|quarantine|execute|submit|register)",
    re.IGNORECASE,
)

for pattern in DENY_BASH_PATTERNS:
    if command and re.search(pattern, command, flags=re.IGNORECASE):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "标准化审查智能体只允许只读审查和报告输出，已阻止高风险命令。",
                    }
                },
                ensure_ascii=False,
            )
        )
        sys.exit(0)

if tool_name.startswith("mcp__") and DENY_MCP_NAME.search(tool_name):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "标准化审查智能体不执行写入、更新、删除、下发或注册类 MCP 操作。",
                }
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

sys.exit(0)
