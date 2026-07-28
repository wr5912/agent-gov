from __future__ import annotations

import asyncio
import json

from app.runtime.managed_claude_events import AgentGovControlEvent, ClaudeSdkMessageEvent
from app.runtime.openai_responses_stream import iter_responses_sse


def _events():
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        StreamEvent,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    return [
        AgentGovControlEvent(
            name="session",
            data={"run_id": "run-sdk", "session_id": "session-sdk"},
        ),
        ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="thinking",
                session_id="sdk-session",
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "分析"},
                },
            )
        ),
        ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="tool-start",
                session_id="sdk-session",
                event={
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {},
                    },
                },
            )
        ),
        ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="tool-delta",
                session_id="sdk-session",
                event={
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"command":"pwd"}',
                    },
                },
            )
        ),
        ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="tool-stop",
                session_id="sdk-session",
                event={"type": "content_block_stop", "index": 1},
            )
        ),
        ClaudeSdkMessageEvent(
            UserMessage(
                content=[ToolResultBlock(tool_use_id="tool-1", content="/workspace")],
            )
        ),
        ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="text",
                session_id="sdk-session",
                event={
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "text_delta", "text": "完成"},
                },
            )
        ),
        ClaudeSdkMessageEvent(
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="分析", signature="signature"),
                    ToolUseBlock(id="tool-1", name="Bash", input={"command": "pwd"}),
                    TextBlock(text="完成"),
                ],
                model="test",
                session_id="sdk-session",
            )
        ),
        ClaudeSdkMessageEvent(
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="sdk-session",
                result="完成",
            )
        ),
        AgentGovControlEvent(
            name="result",
            data={
                "run_id": "run-sdk",
                "session_id": "session-sdk",
                "sdk_session_id": "sdk-session",
                "errors": [],
                "agent_activity": {},
            },
        ),
        AgentGovControlEvent(name="done", data={}),
    ]


async def _source():
    for event in _events():
        yield event


def _collect(*, control: bool) -> list[tuple[str, dict]]:
    async def collect() -> str:
        chunks = [
            chunk
            async for chunk in iter_responses_sse(
                _source(),
                model="test",
                effective_agent_id="agent",
                control=control,
            )
        ]
        return "".join(chunks)

    parsed: list[tuple[str, dict]] = []
    for block in asyncio.run(collect()).split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if name and isinstance(data, dict):
            parsed.append((name, data))
    return parsed


def test_responses_projects_reasoning_lifecycle_and_stable_output_items() -> None:
    events = _collect(control=True)
    names = [name for name, _ in events]

    assert "response.reasoning_text.delta" in names
    assert "response.reasoning_text.done" in names
    assert names.index("response.reasoning_text.done") < names.index("response.output_text.delta")
    completed = next(data["response"] for name, data in events if name == "response.completed")
    assert [item["type"] for item in completed["output"]] == ["reasoning", "message"]
    assert [item["id"] for item in completed["output"]] == ["rs_run-sdk", "msg_run-sdk"]
    assert completed["output"][0]["content"][0]["text"] == "分析"
    assert completed["output"][1]["content"][0]["text"] == "完成"


def test_responses_emits_server_tool_observations_without_function_call_items() -> None:
    events = _collect(control=True)
    names = [name for name, _ in events]

    assert "agentgov.tool_call.started" in names
    assert "agentgov.tool_call.arguments.delta" in names
    assert "agentgov.tool_call.arguments.done" in names
    assert "agentgov.tool_call.result" in names
    assert "agentgov.tool_step" in names
    assert all("function_call" not in name for name in names)


def test_strict_responses_keeps_agentgov_tool_observations_out() -> None:
    events = _collect(control=False)
    names = [name for name, _ in events]

    assert all(not name.startswith("agentgov.") for name in names)
    assert "response.reasoning_text.delta" in names
    assert "response.output_text.delta" in names
