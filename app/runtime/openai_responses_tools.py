from __future__ import annotations

import json
from dataclasses import dataclass, field

from .json_types import JsonObject
from .managed_claude_events import stream_event_payload

ToolObservation = tuple[str, JsonObject]


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass
class _ToolCallState:
    tool_call_id: str
    name: str
    arguments: str = ""


@dataclass
class ServerToolObservationProjector:
    """Project server-executed Claude tools without creating client function calls."""

    calls_by_index: dict[int, _ToolCallState] = field(default_factory=dict)
    started_ids: set[str] = field(default_factory=set)
    completed_ids: set[str] = field(default_factory=set)

    def content_block_start(self, event: JsonObject) -> list[ToolObservation]:
        block = event.get("content_block")
        index = event.get("index")
        if not isinstance(block, dict) or block.get("type") != "tool_use" or not isinstance(index, int):
            return []
        tool_call_id = _str(block.get("id"))
        name = _str(block.get("name"))
        if not tool_call_id or not name:
            return []
        self.calls_by_index[index] = _ToolCallState(tool_call_id=tool_call_id, name=name)
        if tool_call_id in self.started_ids:
            return []
        self.started_ids.add(tool_call_id)
        return [
            (
                "agentgov.tool_call.started",
                {
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                },
            )
        ]

    def arguments_delta(self, message: object, delta: str) -> list[ToolObservation]:
        event = stream_event_payload(message) or {}
        index = event.get("index")
        state = self.calls_by_index.get(index) if isinstance(index, int) else None
        if state is None:
            return []
        state.arguments += delta
        return [
            (
                "agentgov.tool_call.arguments.delta",
                {
                    "tool_call_id": state.tool_call_id,
                    "name": state.name,
                    "delta": delta,
                },
            )
        ]

    def content_block_stop(self, event: JsonObject) -> list[ToolObservation]:
        index = event.get("index")
        state = self.calls_by_index.pop(index, None) if isinstance(index, int) else None
        if state is None or state.tool_call_id in self.completed_ids:
            return []
        self.completed_ids.add(state.tool_call_id)
        parsed: object = state.arguments
        if state.arguments:
            try:
                parsed = json.loads(state.arguments)
            except json.JSONDecodeError:
                parsed = state.arguments
        else:
            parsed = {}
        return [
            (
                "agentgov.tool_call.arguments.done",
                {
                    "tool_call_id": state.tool_call_id,
                    "name": state.name,
                    "arguments": state.arguments,
                    "input": parsed,
                },
            )
        ]

    def complete_content(self, raw: JsonObject) -> list[ToolObservation]:
        content = raw.get("content")
        if not isinstance(content, list):
            return []
        observations: list[ToolObservation] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            tool_use_id = _str(block.get("tool_use_id"))
            if tool_use_id:
                observations.append(
                    (
                        "agentgov.tool_call.result",
                        {
                            "tool_call_id": tool_use_id,
                            "result": block.get("content"),
                            "is_error": bool(block.get("is_error")),
                        },
                    )
                )
                continue
            block_id = _str(block.get("id"))
            name = _str(block.get("name"))
            if not block_id or not name or "input" not in block:
                continue
            if block_id not in self.started_ids:
                self.started_ids.add(block_id)
                observations.append(
                    (
                        "agentgov.tool_call.started",
                        {
                            "tool_call_id": block_id,
                            "name": name,
                            "input": block.get("input"),
                        },
                    )
                )
            if block_id not in self.completed_ids:
                self.completed_ids.add(block_id)
                arguments = json.dumps(
                    block.get("input"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                observations.append(
                    (
                        "agentgov.tool_call.arguments.done",
                        {
                            "tool_call_id": block_id,
                            "name": name,
                            "arguments": arguments,
                            "input": block.get("input"),
                        },
                    )
                )
        return observations
