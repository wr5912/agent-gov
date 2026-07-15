from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .json_types import JsonObject

# Boundary adapter: project the Claude Code agent's own session transcript (read via the
# claude-agent-sdk session API) into the API contract. Per the project's core architecture
# principle, the SDK / agent session is the single source of truth; this module does NOT
# persist a parallel copy — it reads and projects on demand.

_REDACTED = "[redacted]"


def normalize_message(message: object) -> JsonObject:
    """Project one SDK ``SessionMessage`` into the API shape.

    ``SessionMessage.type`` carries the role (``user`` / ``assistant``); ``message.content`` is
    already an Anthropic block list (``thinking`` / ``text`` / ``tool_use`` / ``tool_result``)
    and is passed through faithfully (``tool_use.id`` <-> ``tool_result.tool_use_id`` preserved).
    """
    inner = getattr(message, "message", None)
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, list):
        blocks = content
    elif isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    else:
        blocks = []
    return {
        "uuid": getattr(message, "uuid", None),
        "role": getattr(message, "type", None),
        "parent_tool_use_id": getattr(message, "parent_tool_use_id", None),
        "blocks": blocks,
    }


def _scrub_block(block: object) -> object:
    if not isinstance(block, dict):
        return block
    scrubbed = dict(block)
    for key in ("text", "thinking"):
        if isinstance(scrubbed.get(key), str):
            scrubbed[key] = _REDACTED
    if "input" in scrubbed:  # tool_use arguments
        scrubbed["input"] = _REDACTED
    if "content" in scrubbed:  # tool_result payload
        scrubbed["content"] = _REDACTED
    return scrubbed


def _scrub_message(message: JsonObject) -> JsonObject:
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        return message
    return {**message, "blocks": [_scrub_block(block) for block in blocks]}


async def read_session_history(
    *,
    sdk_store: Any,
    sdk_session_id: str,
    workspace_dir: str | Path,
    scrub: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
) -> JsonObject:
    """从 committed SDK SessionStore 读取并投影会话历史。

    Reuses the claude-agent-sdk session API (``get_session_info`` / ``get_session_messages`` /
    ``list_subagents`` / ``get_subagent_messages``) so the agent's transcript stays the single
    source of truth. Returns ``{sdk_session_id, title, messages[], subagents[]}``.
    """
    import claude_agent_sdk as sdk  # lazy: heavy import, only needed for this read

    directory = str(workspace_dir)
    info = await sdk.get_session_info_from_store(sdk_store, sdk_session_id, directory=directory)
    messages = await sdk.get_session_messages_from_store(
        sdk_store,
        sdk_session_id,
        directory=directory,
        limit=limit,
        offset=offset,
    )
    agent_ids = await sdk.list_subagents_from_store(sdk_store, sdk_session_id, directory=directory)
    subagent_messages = [
        (
            agent_id,
            await sdk.get_subagent_messages_from_store(
                sdk_store,
                sdk_session_id,
                agent_id,
                directory=directory,
            ),
        )
        for agent_id in agent_ids
    ]

    normalized = [normalize_message(message) for message in messages]
    subagents = [
        {"agent_id": agent_id, "messages": [normalize_message(message) for message in agent_messages]}
        for agent_id, agent_messages in subagent_messages
    ]
    if scrub:
        normalized = [_scrub_message(message) for message in normalized]
        subagents = [{**agent, "messages": [_scrub_message(m) for m in agent["messages"]]} for agent in subagents]

    title = None
    if info is not None:
        title = getattr(info, "custom_title", None) or getattr(info, "summary", None)
    return {"sdk_session_id": sdk_session_id, "title": title, "messages": normalized, "subagents": subagents}
