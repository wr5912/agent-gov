from __future__ import annotations

import asyncio
import json

import pytest
from app.runtime.managed_claude_events import (
    AgentGovControlEvent,
    ClaudeSdkMessageEvent,
)
from app.runtime.message_utils import extract_answer_from_messages
from app.runtime.openai_responses_adapter import (
    RESPONSE_MODE_MARKER_KEY,
    extract_reasoning_from_messages,
    response_from_run_payload,
)
from app.runtime.openai_responses_schemas import ResponsesRequest
from app.runtime.openai_responses_stream import iter_responses_sse
from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock
from pydantic import ValidationError


def _messages() -> list[dict[str, object]]:
    return [
        {
            "event": "AssistantMessage",
            "parent_tool_use_id": "tool-parent",
            "content": [
                {"thinking": "子 Agent 推理"},
                {"text": "子 Agent 回答"},
            ],
        },
        {
            "event": "AssistantMessage",
            "parent_tool_use_id": None,
            "content": [
                {"thinking": "主 Agent 推理"},
                {"text": "主 Agent 回答"},
            ],
        },
        {
            "event": "ResultMessage:success",
            "result": "主 Agent 回答",
        },
    ]


def test_persisted_answer_and_reasoning_ignore_subagent_content() -> None:
    messages = _messages()

    assert extract_answer_from_messages(messages) == "主 Agent 回答"
    assert extract_reasoning_from_messages(messages) == "主 Agent 推理"

    strict = response_from_run_payload(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "messages": messages,
            "metadata": {RESPONSE_MODE_MARKER_KEY: "strict"},
            "errors": [],
        }
    )
    assert [item.type for item in strict.output] == ["reasoning", "message"]
    assert strict.output[-1].content[0].text == "主 Agent 回答"
    assert strict.agentgov is None


def test_result_is_only_fallback_when_no_top_level_assistant_exists() -> None:
    messages = [
        {
            "event": "AssistantMessage",
            "parent_tool_use_id": "tool-parent",
            "content": [{"text": "子 Agent 回答"}],
        },
        {
            "event": "ResultMessage:success",
            "result": "最终结果兜底",
        },
    ]

    assert extract_answer_from_messages(messages) == "最终结果兜底"


@pytest.mark.parametrize(
    "payload",
    [
        {"input": ""},
        {"input": []},
        {
            "input": [
                {
                    "type": "message",
                    "role": "system",
                    "content": "only system",
                }
            ]
        },
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "image_url", "url": "x"}],
                }
            ]
        },
        {"input": "hello", "reasoning": {"effort": "high"}},
        {"input": "hello", "temperature": 0.5},
        {"input": "hello", "tools": []},
    ],
)
def test_responses_request_allowlist_and_typed_input_fail_closed(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ResponsesRequest.model_validate(payload)


def _events(text: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in text.split("\n\n"):
        name: str | None = None
        data: dict[str, object] | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if name and data is not None:
            events.append((name, data))
    return events


def test_stream_standard_output_is_main_only_while_control_trace_retains_subagent() -> None:
    async def source():
        yield AgentGovControlEvent(
            name="session",
            data={"run_id": "run-1", "session_id": "session-1"},
        )
        yield ClaudeSdkMessageEvent(
            AssistantMessage(
                content=[
                    ThinkingBlock(
                        thinking="子 Agent 推理",
                        signature="signature",
                    ),
                    TextBlock(text="子 Agent 回答"),
                ],
                model="test",
                message_id="msg-sub",
                parent_tool_use_id="tool-parent",
            )
        )
        yield ClaudeSdkMessageEvent(
            AssistantMessage(
                content=[
                    ThinkingBlock(
                        thinking="主 Agent 推理",
                        signature="signature",
                    ),
                    TextBlock(text="主 Agent 回答"),
                ],
                model="test",
                message_id="msg-main",
            )
        )
        yield AgentGovControlEvent(
            name="result",
            data={
                "run_id": "run-1",
                "session_id": "session-1",
                "errors": [],
            },
        )
        yield AgentGovControlEvent(name="done", data={})

    async def scenario() -> str:
        return "".join(
            [
                chunk
                async for chunk in iter_responses_sse(
                    source(),
                    model="test",
                    effective_agent_id="agent-1",
                    control=True,
                    include_trace=True,
                )
            ]
        )

    events = _events(asyncio.run(scenario()))
    names = [name for name, _ in events]
    standard_deltas = [
        data["delta"]
        for name, data in events
        if name
        in {
            "response.reasoning_text.delta",
            "response.output_text.delta",
        }
    ]
    traces = [data["payload"] for name, data in events if name == "agentgov.trace_event"]

    assert standard_deltas == ["主 Agent 推理", "主 Agent 回答"]
    assert any(trace["scope"] == "subagent" and trace["parent_tool_use_id"] == "tool-parent" for trace in traces)
    assert any(trace["scope"] == "main" for trace in traces)
    completed = dict(events)["response.completed"]["response"]
    assert "主 Agent 回答" in json.dumps(completed, ensure_ascii=False)
    assert "子 Agent 回答" not in json.dumps(completed, ensure_ascii=False)
    assert names[-1] == "response.completed"
