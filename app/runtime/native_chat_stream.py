from __future__ import annotations

from .agent_trace import AgentTraceProjector
from .json_types import JsonObject


class NativeChatSemanticProjector:
    """Keep answer streaming while replacing complete raw-message noise with Trace facts."""

    def __init__(self) -> None:
        self.trace_projector: AgentTraceProjector | None = None

    def project(self, frame: JsonObject) -> list[JsonObject]:
        event = frame.get("event")
        data = frame.get("data")
        data = data if isinstance(data, dict) else {}
        if event == "session":
            run_id = data.get("run_id")
            if isinstance(run_id, str) and run_id:
                self.trace_projector = AgentTraceProjector(run_id)
            return [frame]
        if event != "message":
            return [frame]
        scope = data.get("scope")
        if data.get("text_kind") == "delta":
            delta_event = data.get("event")
            if delta_event not in {
                "StreamEvent:text_delta",
                "StreamEvent:thinking_delta",
                "StreamEvent",
            }:
                return []
            return [frame] if scope != "subagent" and isinstance(data.get("text"), str) and data.get("text") else []
        if data.get("event") == "StreamEvent":
            return []

        projected: list[JsonObject] = []
        text = data.get("text")
        if scope != "subagent" and isinstance(text, str) and text:
            projected.append(frame)
        raw = data.get("raw")
        if not isinstance(raw, dict) or self.trace_projector is None:
            return projected
        event_name = data.get("event")
        projection_input = raw if isinstance(raw.get("event"), str) else {**raw, "event": event_name}
        projected.extend({"event": "trace_event", "data": event.model_dump(mode="json")} for event in self.trace_projector.project_message(projection_input))
        return projected
