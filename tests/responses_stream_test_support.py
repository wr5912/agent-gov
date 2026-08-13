from __future__ import annotations

import asyncio
import json
from dataclasses import make_dataclass

from app.runtime import claude_prompt_suggestions
from app.runtime.async_iterators import close_async_iterator
from app.runtime.managed_claude_events import (
    AgentGovControlEvent,
    AgentGovHeartbeatEvent,
    ClaudeSdkMessageEvent,
)
from app.runtime.schemas import ChatRequest
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _base_load_app
from workspace_package_test_utils import import_new_agent as _import_new_agent

SESSION_FRAME = {
    "event": "session",
    "data": {
        "run_id": "run-9",
        "session_id": "sess-9",
        "sdk_session_id": "sdk-9",
        "agent_version_id": "ver-9",
        "agent_id": "soc-ops",
        "langfuse_trace_id": "trace-9",
        "langfuse_trace_url": "http://langfuse-web:3000/project/agent-gov/traces/trace-9",
    },
}
ASSISTANT_FRAME = {
    "event": "message",
    "data": {"event": "AssistantMessage", "text": "日报正文", "raw": {}},
}
RESULT_FRAME = {
    "event": "result",
    "data": {
        "run_id": "run-9",
        "session_id": "sess-9",
        "sdk_session_id": "sdk-9",
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "stop_reason": "end_turn",
        "errors": [],
        "agent_activity": {},
    },
}
DONE_FRAME = {"event": "done", "data": "[DONE]"}


def load_app(monkeypatch, tmp_path, **kwargs):
    return _base_load_app(
        monkeypatch,
        tmp_path,
        requires_web_hitl=False,
        **kwargs,
    )


def patch_sdk_query(monkeypatch, fake_query) -> None:
    """Install the same fake behind one-shot and bidirectional SDK drivers."""
    import claude_agent_sdk

    class FakeClaudeSDKClient:
        def __init__(self, *, options, transport=None):
            self.options = options
            self.responses = None
            self.control_task = None

        async def connect(self, control_stream):
            async def consume_control_stream():
                async for _ in control_stream:
                    pass

            self.control_task = asyncio.create_task(consume_control_stream())
            await asyncio.sleep(0)
            assert not self.control_task.done()

        async def query(self, prompt, session_id="default"):
            self.responses = fake_query(prompt=prompt, options=self.options)

        async def receive_response(self):
            from claude_agent_sdk import ResultMessage

            assert self.responses is not None
            async for message in self.responses:
                yield message
                if isinstance(message, ResultMessage):
                    return

        async def disconnect(self):
            if self.responses is not None:
                await close_async_iterator(self.responses)
            assert self.control_task is not None
            await asyncio.wait_for(self.control_task, timeout=1)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(claude_prompt_suggestions, "query_with_prompt_suggestions", fake_query)
    monkeypatch.setattr(claude_prompt_suggestions, "PromptSuggestionClaudeClient", FakeClaudeSDKClient)


def fake_sdk_query_success(entry_label: str = "sdk-race"):
    """真实 stream 全链用的 fake SDK query：yield SDK 消息并走持久化。"""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {
                "project_key": options.session_store.binding.project_key,
                "session_id": sdk_session_id,
            },
            [{"type": "user", "uuid": f"{entry_label}-entry"}],
        )
        yield AssistantMessage(
            content=[TextBlock(text="收到")],
            model="<synthetic>",
            session_id=sdk_session_id,
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
            result="收到",
        )

    return fake_query


def drive_stream(module, req: ChatRequest, on_event=None) -> list:
    events: list = []

    async def go():
        async for event in module.runtime.stream(req):
            events.append(event)
            if on_event is not None:
                on_event(event)

    asyncio.run(go())
    return events


def _managed_sdk_message(data: dict) -> ClaudeSdkMessageEvent:
    from claude_agent_sdk import (
        AssistantMessage,
        StreamEvent,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    sdk_event = str(data.get("event") or "SdkMessage")
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    raw = dict(data.get("raw")) if isinstance(data.get("raw"), dict) else {}
    if data.get("text_kind") == "delta" or sdk_event == "StreamEvent":
        return ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="legacy-test-delta",
                session_id="sdk-9",
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
    class_name, _, subtype = sdk_event.partition(":")
    if class_name == "AssistantMessage":
        content = []
        for block in raw.get("content", []):
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("thinking"), str):
                content.append(
                    ThinkingBlock(
                        thinking=block["thinking"],
                        signature=str(block.get("signature") or ""),
                    )
                )
            elif isinstance(block.get("text"), str):
                content.append(TextBlock(text=block["text"]))
            elif isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
                content.append(
                    ToolUseBlock(
                        id=block["id"],
                        name=block["name"],
                        input=block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
                )
        if not content and text:
            content.append(TextBlock(text=text))
        return ClaudeSdkMessageEvent(AssistantMessage(content=content, model="legacy-test", session_id="sdk-9"))
    if class_name == "UserMessage":
        content = [
            ToolResultBlock(
                tool_use_id=str(block.get("tool_use_id")),
                content=block.get("content"),
                is_error=bool(block.get("is_error")),
            )
            for block in raw.get("content", [])
            if isinstance(block, dict) and block.get("tool_use_id")
        ]
        return ClaudeSdkMessageEvent(UserMessage(content=content))

    raw.pop("event", None)
    if subtype:
        raw.setdefault("subtype", subtype)
    message_type = make_dataclass(class_name, [(key, object) for key in raw])
    return ClaudeSdkMessageEvent(message_type(**raw))


def as_managed(frame):
    event = frame.get("event")
    data = frame.get("data")
    data = data if isinstance(data, dict) else {}
    if event == "heartbeat":
        return AgentGovHeartbeatEvent(
            run_id=str(data.get("run_id") or "run-9"),
            timestamp=str(data.get("timestamp") or ""),
        )
    if event != "message":
        return AgentGovControlEvent(name=str(event), data=data)
    return _managed_sdk_message(data)


def parse_sse(sse_text: str):
    events = []
    for block in sse_text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        name = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name is not None and name not in {
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_text.done",
        }:
            events.append((name, data))
    return events


def fake_stream(frames):
    async def stream(req, *, profile=None, **kwargs):
        for frame in frames:
            yield as_managed(frame)

    return stream


def fake_capturing_stream(captured: dict, frames):
    async def stream(req, *, profile=None, **kwargs):
        captured["req"] = req
        captured["profile"] = profile
        for frame in frames:
            yield as_managed(frame)

    return stream


def register_business_agent(client: TestClient, agent_id: str = "soc-ops") -> None:
    assert (
        _import_new_agent(
            client,
            agent_id=agent_id,
            name="客服",
            requires_web_hitl=False,
        ).status_code
        == 200
    )
