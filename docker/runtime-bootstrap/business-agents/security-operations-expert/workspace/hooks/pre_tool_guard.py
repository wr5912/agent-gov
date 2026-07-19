#!/usr/bin/env python3
"""Fail-closed Claude Code PreToolUse guard for business-agent Bash calls."""

import json
import re
import sys

DENY_PATTERNS = (
    r"\brm\s+-rf\s+/(?:\*|\s|$)",
    r"\bdd\s+if=.*\s+of=/dev/",
    r"\bmkfs(?:\.|\s)",
    r"\b(?:shutdown|reboot|poweroff)\b",
    r"\biptables\b.*\s-F\b",
    r"\bkubectl\s+(?:delete|drain|cordon|apply|patch|replace|scale|rollout)\b",
    r"\bterraform\s+apply\b",
    r"\bansible-playbook\b.*(?:--limit\s+(?:all|production|prod)|production|prod)",
    r"\bsystemctl\s+(?:restart|stop)\b",
    r"\b(?:nmap|masscan)\b.*(?:-sS|-sT|-A|--script)",
    r"\bdocker\s+(?:rm|rmi|system\s+prune|volume\s+rm)\b",
    r"\b(?:ssh|scp)\b",
    r"\bcurl\b.*\|\s*(?:bash|sh)\b",
    r"\bwget\b.*\|\s*(?:bash|sh)\b",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
)


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_name"), str):
            raise ValueError("expected a PreToolUse object with tool_name")
        if payload["tool_name"] != "Bash":
            return 0
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
            raise ValueError("expected tool_input.command string")
        command = tool_input["command"]
    except Exception as exc:
        print(f"PreToolUse safety validation failed closed: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    if any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in DENY_PATTERNS):
        _deny("A destructive or remote command was denied by the workspace safety policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
