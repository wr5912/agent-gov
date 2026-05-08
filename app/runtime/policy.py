from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny


DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bkubectl\s+(delete|drain|cordon|apply|patch|replace)\b",
    r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+rm)\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\bcurl\b.*\|\s*(bash|sh)",
    r"\bwget\b.*\|\s*(bash|sh)",
]


def _is_dangerous_bash(command: str) -> str | None:
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return pattern
    return None


async def guard_tool_use(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
    """SDK can_use_tool callback.

    This is only invoked when a tool would otherwise ask for permission. Tools already
    allowed by settings/allowed_tools do not hit this callback; use hooks for full audit.
    """
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        matched = _is_dangerous_bash(command)
        if matched:
            return PermissionResultDeny(
                message=f"Blocked dangerous Bash command by policy: pattern={matched}",
                interrupt=True,
            )
    return PermissionResultAllow()


async def pre_tool_use_hook(input_data: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]) -> dict[str, Any]:
    """PreToolUse hook used for deterministic runtime enforcement."""
    tool_name = input_data.get("tool_name")
    tool_input = input_data.get("tool_input") or {}
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        matched = _is_dangerous_bash(command)
        if matched:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Blocked by container policy: {matched}",
                }
            }
    return {}


def build_default_hooks() -> dict[str, list[HookMatcher]]:
    return {
        "PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_tool_use_hook])],
    }
