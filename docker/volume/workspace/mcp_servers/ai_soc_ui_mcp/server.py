#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-soc-ui")


@mcp.tool()
def emit_a2ui_message(message: Any) -> dict[str, Any]:
    """Emit one raw A2UI v0.9 server-to-client message.

    Clean-room v0.9 entry point. Pass exactly one complete A2UI v0.9 message,
    not an array and not a quoted JSON string. Valid message types are:
    createSurface, updateComponents, updateDataModel, and deleteSurface.

    Do not print the payload in the user-facing answer.
    """
    message_type = "unknown"
    surface_id = None
    if isinstance(message, dict):
        for key in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface"):
            payload = message.get(key)
            if isinstance(payload, dict):
                message_type = key
                surface_id = payload.get("surfaceId")
                break
    return {
        "ok": True,
        "version": "v0.9",
        "message_type": message_type,
        "surface_id": surface_id,
        "note": "A2UI v0.9 message was captured by the runtime and forwarded through AG-UI.",
    }


@mcp.tool()
def render_a2ui(payload: Any) -> dict[str, Any]:
    """Legacy v0.8/card fallback for rendering an A2UI surface.

    Deprecated for new generated UI. Prefer `emit_a2ui_message` and send one
    raw A2UI v0.9 message per tool call. This helper remains only for
    compatibility with existing card-mode and raw v0.8 flows.

    Supported payload modes:
    - {"mode": "card", "surfaceId": "...", "cards": [...]} for Agent-generated AI-SOC cards.
    - {"mode": "a2ui", "messages": [...]} for advanced raw A2UI v0.8 messages.

    Catalog mode is disabled: the backend must not synthesize business cards.
    Generate the card title, sections, tables, metrics, evidence, and actions
    yourself and pass them through mode "card".

    Do not print the payload in the user-facing answer.
    """
    mode = "a2ui"
    item_count = 1
    if isinstance(payload, dict):
        mode = str(payload.get("mode") or mode)
        if isinstance(payload.get("cards"), list):
            item_count = len(payload["cards"])
        elif isinstance(payload.get("messages"), list):
            item_count = len(payload["messages"])
        elif isinstance(payload.get("components"), list):
            item_count = len(payload["components"])
        elif isinstance(payload.get("component"), dict):
            item_count = 1
        elif isinstance(payload.get("payload"), dict):
            nested = payload["payload"]
            if isinstance(nested.get("cards"), list):
                item_count = len(nested["cards"])
            elif isinstance(nested.get("messages"), list):
                item_count = len(nested["messages"])
            elif isinstance(nested.get("components"), list):
                item_count = len(nested["components"])
            elif isinstance(nested.get("component"), dict):
                item_count = 1
    return {
        "ok": True,
        "mode": mode,
        "item_count": item_count,
        "note": "render_a2ui payload was captured by the runtime and forwarded through AG-UI.",
    }


@mcp.tool()
def emit_cards(cards: Any, surfaceId: str = "ai-soc-generated-cards") -> dict[str, Any]:
    """Legacy helper that converts AI-SOC cards into A2UI v0.8 messages.

    Deprecated for new generated UI. Prefer `emit_a2ui_message` and send one
    raw A2UI v0.9 message per tool call. Use this only as a migration fallback
    when v0.9 UI is unavailable. Pass `cards` as an array of card specs, not as
    a quoted JSON string:
    [{"title": "...", "subtitle": "...", "sections": [...]}].

    Supported section types: metric_group, table, key_value, tags, action_list,
    or plain text lists. Card specs may also include an `actions` array for
    frontend interactions, for example:
    [{"label": "查看资产", "name": "ai_soc.asset.select", "context": {"assetId": "vpn-05"}}].
    Supported non-destructive action names are:
    - ai_soc.asset.select
    - ai_soc.alert.select
    - ai_soc.evidence.select
    - ai_soc.judgement.request
    The runtime converts these cards into A2UI v0.8 messages and forwards them
    through AG-UI.
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
    """Legacy helper that emits raw A2UI v0.8 messages to the AI-SOC frontend.

    Deprecated for new generated UI. Prefer `emit_a2ui_message` and send one
    raw A2UI v0.9 message per tool call. This compatibility tool accepts only
    raw A2UI v0.8 server-to-client messages. Do not pass AI-SOC card specs
    here; use `render_a2ui` mode "card" or `emit_cards`. Pass messages as a
    JSON array, not as a quoted JSON string. Valid A2UI v0.8 messages include:
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
