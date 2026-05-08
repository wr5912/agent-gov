#!/usr/bin/env python3
"""Example external hook script.

Claude Code settings can call this as a hook. The FastAPI runtime also includes
in-process SDK hooks, so this file is mainly a template for workspace-owned policy.
"""

import json
import re
import sys

payload = json.load(sys.stdin)
tool_name = payload.get("tool_name")
tool_input = payload.get("tool_input") or {}

if tool_name == "Bash":
    command = str(tool_input.get("command") or "")
    blocked = [r"\brm\s+-rf\b", r"\bssh\b", r"\bkubectl\s+delete\b", r"\bdocker\s+rm\b"]
    for pattern in blocked:
        if re.search(pattern, command, flags=re.IGNORECASE):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Blocked by workspace hook: {pattern}"
                }
            }, ensure_ascii=False))
            sys.exit(0)

print("{}")
