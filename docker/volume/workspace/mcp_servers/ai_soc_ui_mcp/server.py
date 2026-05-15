#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-soc-ui")


@mcp.tool()
def emit_cards(cards: Any, surfaceId: str = "ai-soc-generated-cards") -> dict[str, Any]:
    """Emit AI-SOC UI cards to the frontend.

    This is the preferred tool for normal AI-SOC answers. Pass `cards` as an
    array of card specs, not as a quoted JSON string:
    [{"title": "...", "subtitle": "...", "sections": [...]}].

    Supported section types: metric_group, table, key_value, tags, action_list,
    or plain text lists. The runtime converts these cards into A2UI v0.8
    messages and forwards them through AG-UI.
    """
    card_count = len(cards) if isinstance(cards, list) else 1
    return {
        "ok": True,
        "surface_id": surfaceId,
        "card_count": card_count,
        "note": "AI-SOC cards were captured by the runtime and forwarded as A2UI through AG-UI.",
    }


@mcp.tool()
def emit_a2ui(messages: Any) -> dict[str, Any]:
    """Emit raw A2UI v0.8 messages to the AI-SOC frontend.

    Use this advanced tool only when the simplified `emit_cards` tool cannot
    express the required UI. Pass messages as a JSON array, not as a quoted JSON
    string. Valid A2UI v0.8 server-to-client messages include:
    [{"beginRendering": {...}}, {"surfaceUpdate": {...}}].
    """
    message_count = len(messages) if isinstance(messages, list) else 1
    return {
        "ok": True,
        "message_count": message_count,
        "note": "A2UI messages were captured by the runtime and forwarded through AG-UI.",
    }


if __name__ == "__main__":
    mcp.run()
