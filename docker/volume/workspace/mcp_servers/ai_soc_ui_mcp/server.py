#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, model_validator

mcp = FastMCP("ai-soc-ui")


class CreateSurfaceMessage(BaseModel):
    surfaceId: str = Field(..., min_length=1, description="Required surface id for the UI surface.")
    catalogId: str = Field(
        ...,
        min_length=1,
        description="Use https://a2ui.org/specification/v0_9/basic_catalog.json.",
    )
    sendDataModel: bool | None = Field(default=None, description="Whether the client should send a data model.")


class UpdateComponentsMessage(BaseModel):
    surfaceId: str = Field(
        ...,
        min_length=1,
        description="Required on every updateComponents message; use the same value as createSurface.surfaceId.",
    )
    components: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        description=(
            "Non-empty array of A2UI basic catalog component objects. Do not use an object keyed by id. "
            "For a complete surface update, include a root component with id 'root'."
        ),
    )


class UpdateDataModelMessage(BaseModel):
    surfaceId: str = Field(
        ...,
        min_length=1,
        description="Required on every updateDataModel message; use the same value as createSurface.surfaceId.",
    )
    path: str | None = Field(default=None, description="Optional data model path.")
    value: Any = Field(default=None, description="Data model value.")
    data: Any = Field(default=None, description="Legacy-compatible data value; prefer value for new messages.")


class DeleteSurfaceMessage(BaseModel):
    surfaceId: str = Field(..., min_length=1, description="Required surface id to delete.")


class A2uiV09Message(BaseModel):
    version: Literal["v0.9"] = Field(..., description="Must be exactly v0.9.")
    createSurface: CreateSurfaceMessage | None = None
    updateComponents: UpdateComponentsMessage | None = None
    updateDataModel: UpdateDataModelMessage | None = None
    deleteSurface: DeleteSurfaceMessage | None = None

    @model_validator(mode="after")
    def require_exactly_one_message(self) -> "A2uiV09Message":
        present = [
            self.createSurface,
            self.updateComponents,
            self.updateDataModel,
            self.deleteSurface,
        ]
        if sum(item is not None for item in present) != 1:
            raise ValueError(
                "A2UI v0.9 message must contain exactly one of createSurface, "
                "updateComponents, updateDataModel, or deleteSurface."
            )
        return self


@mcp.tool()
def emit_a2ui_message(message: A2uiV09Message) -> dict[str, Any]:
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
    updateComponents.surfaceId is required on every update. It is not inherited
    from createSurface. updateComponents.components must be a non-empty array
    of component objects, not an object keyed by component id.
    Complete surface updates must include a component with id "root" so the
    frontend has a render entry point.

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
    if isinstance(message, BaseModel):
        message = message.model_dump(exclude_none=True)
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
