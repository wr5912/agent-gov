#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-soc-ui")


@mcp.tool()
def render_a2ui(payload: Any) -> dict[str, Any]:
    """Render an A2UI surface through the AI-SOC AG-UI bridge.

    Preferred official-aligned entry point for structured UI.

    Supported payload modes:
    - {"mode": "card", "surfaceId": "...", "cards": [...]} for normal AI-SOC cards.
    - {"mode": "a2ui", "messages": [...]} for advanced raw A2UI v0.8 messages.
    - {"mode": "catalog", "catalog": "ai-soc", "component": {"type": "...", "props": {...}}}
      for trusted AI-SOC catalog components. Supported component types:
      RiskMetricGroup, RiskAssetTable, AlertTriageCard.

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
    """Emit AI-SOC UI cards to the frontend.

    Compatibility helper for normal AI-SOC answers. Prefer `render_a2ui` with
    mode "card" when available. Pass `cards` as an
    array of card specs, not as a quoted JSON string:
    [{"title": "...", "subtitle": "...", "sections": [...]}].

    Supported section types: metric_group, table, key_value, tags, action_list,
    or plain text lists. Card specs may also include an `actions` array for
    frontend interactions, for example:
    [{"label": "查看资产", "name": "ai_soc.asset.select", "context": {"assetId": "vpn-05"}}].
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
    """Emit raw A2UI v0.8 messages to the AI-SOC frontend.

    Compatibility helper for advanced raw A2UI. Prefer `render_a2ui` with mode
    "a2ui" when available. Pass messages as a JSON array, not as a quoted JSON
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
