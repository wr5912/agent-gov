from __future__ import annotations

import asyncio
import json

from app.runtime.openai_responses_stream import iter_responses_sse

SESSION = {
    "event": "session",
    "data": {"run_id": "run-trace", "session_id": "session-trace"},
}
DONE = {"event": "done", "data": "[DONE]"}


async def _frames(items):
    for item in items:
        yield item


def _collect(items, **kwargs) -> str:
    async def consume() -> str:
        chunks = []
        async for chunk in iter_responses_sse(_frames(items), **kwargs):
            chunks.append(chunk)
        return "".join(chunks)

    return asyncio.run(consume())


def _parse(text: str):
    events = []
    for block in text.split("\n\n"):
        event_name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if event_name:
            events.append((event_name, data))
    return events


def test_trace_opt_in_emits_complete_thinking_and_every_tool_block() -> None:
    message = {
        "event": "message",
        "data": {
            "event": "AssistantMessage",
            "text": "done",
            "text_kind": "snapshot",
            "raw": {
                "event": "AssistantMessage",
                "content": [
                    {"thinking": "完整思考", "signature": "opaque"},
                    {"name": "Glob", "id": "tu-1", "input": {"pattern": "*.py"}},
                    {"name": "Read", "id": "tu-2", "input": {"file_path": "README.md"}},
                    {"text": "done"},
                ],
            },
        },
    }
    counter = {
        "event": "message",
        "data": {
            "event": "SystemMessage:thinking_tokens",
            "text": "",
            "text_kind": "snapshot",
            "raw": {"event": "SystemMessage:thinking_tokens", "subtype": "thinking_tokens"},
        },
    }

    events = _parse(
        _collect(
            [SESSION, counter, message, DONE],
            model="m",
            effective_agent_id="x",
            control=True,
            include_trace=True,
        )
    )
    trace_events = [data["payload"] for name, data in events if name == "agentgov.trace_event"]
    tool_steps = [data["payload"] for name, data in events if name == "agentgov.tool_step"]

    assert [event["kind"] for event in trace_events] == ["thinking", "tool_use", "tool_use", "text"]
    assert [step["tool_name"] for step in tool_steps] == ["Glob", "Read"]
    assert trace_events[0]["payload"] == {"thinking": "完整思考"}


def test_trace_is_opt_in_and_sdk_raw_keeps_assistant_snapshot() -> None:
    assistant = {
        "event": "message",
        "data": {
            "event": "AssistantMessage",
            "text": "answer",
            "text_kind": "snapshot",
            "raw": {"event": "AssistantMessage", "content": [{"text": "answer"}]},
        },
    }

    default_text = _collect([SESSION, assistant, DONE], model="m", effective_agent_id="x", control=True)
    raw_events = _parse(
        _collect(
            [SESSION, assistant, DONE],
            model="m",
            effective_agent_id="x",
            control=True,
            sdk_raw=True,
        )
    )

    assert "event: agentgov.trace_event" not in default_text
    sdk_raw = [data["payload"]["raw"] for name, data in raw_events if name == "agentgov.sdk_raw"]
    assert sdk_raw == [assistant["data"]["raw"]]
