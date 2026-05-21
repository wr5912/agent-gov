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

A2UI_V09_MESSAGE_TOOL_NAME = "mcp__ai-soc-ui__emit_a2ui_message"
LEGACY_A2UI_TOOL_NAMES = {
    "mcp__ai-soc-ui__render_a2ui",
    "mcp__ai-soc-ui__emit_cards",
    "mcp__ai-soc-ui__emit_a2ui",
}


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
    if tool_name in LEGACY_A2UI_TOOL_NAMES:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Legacy A2UI tools are disabled for active AI-SOC UI generation. "
                    "Use mcp__ai-soc-ui__emit_a2ui_message with one structured A2UI v0.9 message."
                ),
            }
        }
    if tool_name == A2UI_V09_MESSAGE_TOOL_NAME:
        rejection = _invalid_a2ui_v09_message_reason(tool_input)
        if rejection:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": rejection,
                }
            }
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


def _invalid_a2ui_v09_message_reason(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return "emit_a2ui_message requires structured tool input with a message object, not a string or array."
    message = tool_input.get("message", tool_input)
    if not isinstance(message, dict):
        return "emit_a2ui_message requires message to be one structured A2UI v0.9 object."
    if message.get("protocol") == "a2ui" or message.get("version") == "v0_8" or "messages" in message:
        return (
            "emit_a2ui_message only accepts A2UI v0.9 messages. Do not send v0.8 envelopes "
            "with protocol/messages; send createSurface, updateComponents, updateDataModel, or deleteSurface."
        )
    if message.get("version") != "v0.9":
        return "emit_a2ui_message requires message.version to be exactly 'v0.9'."
    present_keys = [
        key for key in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface") if key in message
    ]
    if len(present_keys) != 1:
        return (
            "emit_a2ui_message requires exactly one v0.9 message key: createSurface, "
            "updateComponents, updateDataModel, or deleteSurface."
        )
    return None


def build_default_hooks() -> dict[str, list[HookMatcher]]:
    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[pre_tool_use_hook]),
            HookMatcher(matcher=A2UI_V09_MESSAGE_TOOL_NAME, hooks=[pre_tool_use_hook]),
            *[HookMatcher(matcher=tool_name, hooks=[pre_tool_use_hook]) for tool_name in LEGACY_A2UI_TOOL_NAMES],
        ],
    }
