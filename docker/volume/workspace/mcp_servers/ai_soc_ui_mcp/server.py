#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-soc-ui")


@mcp.tool()
def emit_a2ui_message(message: dict[str, Any]) -> dict[str, Any]:
    """Emit one raw A2UI v0.9 server-to-client message.

    Clean-room v0.9 entry point. Pass exactly one complete A2UI v0.9 message,
    not an array and not a quoted JSON string. Valid message types are:
    createSurface, updateComponents, updateDataModel, and deleteSurface.

    Required shape:
    {
      "version": "v0.9",
      "createSurface": {
        "surfaceId": "asset-risk-overview",
        "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
        "sendDataModel": true
      }
    }

    The message object must have "version": "v0.9" and exactly one of the
    supported message keys. Do not use {"type": "createSurface", ...}; that is
    not an A2UI v0.9 message.

    updateComponents must use registered A2UI v0.9 basic catalog components.
    Allowed component names are:
    Text, Image, Icon, Video, AudioPlayer, Row, Column, List, Card, Tabs,
    Divider, Modal, Button, TextField, CheckBox, ChoicePicker, Slider,
    DateTimeInput.

    Preferred AI-SOC generated UI components are:
    Card, Column, Row, Text, List, Divider, Button.

    Use "component": "Card" / "Column" / "Text". Do not use "type".
    Do not use unregistered components such as Table, MetricCard, RiskBadge,
    Chart, Progress, or Badge. Do not use legacy card fields such as sections,
    metric_group, table, rows, or columns. To show table-like asset data before
    a custom catalog exists, render rows as Text items inside Card/Column.

    Keep the interaction bounded: most user requests should emit at most three
    v0.9 UI messages total. For risk overviews, prefer createSurface plus one
    final updateComponents message, then stop.

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

if __name__ == "__main__":
    mcp.run()
