from __future__ import annotations

from typing import TypeAlias

from claude_agent_sdk import HookMatcher

from .json_types import JsonObject

ClaudeHookMap: TypeAlias = dict[str, list[HookMatcher]]


def build_control_keepalive_hooks() -> ClaudeHookMap:
    return {"Stop": [HookMatcher(matcher=None, hooks=[_control_keepalive_hook])]}


async def _control_keepalive_hook(input_data: JsonObject, tool_use_id: str | None, context: JsonObject) -> JsonObject:
    return {}
