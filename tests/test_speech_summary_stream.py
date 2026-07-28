from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from app.routers.chat import create_chat_router
from app.routers.claude_sdk_events import create_claude_sdk_events_router
from app.runtime.managed_claude_events import (
    AgentGovControlEvent,
    AgentGovHeartbeatEvent,
)
from app.runtime.settings import AppSettings
from app.runtime.speech_summary import (
    SpeechSummaryCoordinator,
    SpeechSummaryOutput,
)
from claude_agent_sdk import (
    AssistantMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate(self, **kwargs: object) -> SpeechSummaryOutput:
        self.calls.append(kwargs)
        return SpeechSummaryOutput(text="正在核对告警证据与关键攻击链路")


def _stream_event(
    event: dict[str, object],
    *,
    parent_tool_use_id: str | None = None,
) -> StreamEvent:
    return StreamEvent(
        uuid="stream-1",
        session_id="sdk-1",
        event=event,
        parent_tool_use_id=parent_tool_use_id,
    )


def test_coordinator_uses_native_thinking_stop_and_complete_assistant_boundaries() -> None:
    async def scenario() -> tuple[list[AgentGovControlEvent], _RecordingService]:
        emitted: list[AgentGovControlEvent] = []
        service = _RecordingService()

        async def emit(event: AgentGovControlEvent) -> None:
            emitted.append(event)

        coordinator = SpeechSummaryCoordinator(
            service=service,  # type: ignore[arg-type]
            run_id="run-1",
            boundaries=(
                "thinking_block_completed",
                "assistant_response_completed",
            ),
            enabled=True,
            emit=emit,
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "message_start",
                    "message": {"id": "msg-1"},
                }
            )
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "核对告警",
                    },
                }
            )
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "并关联证据",
                    },
                }
            )
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "content_block_stop",
                    "index": 2,
                }
            )
        )
        await coordinator.drain(1)
        coordinator.observe(
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="不作为正文", signature="signature"),
                    TextBlock(text="第一段答案"),
                    ToolUseBlock(id="tool-1", name="Read", input={}),
                    TextBlock(text="第二段答案"),
                ],
                model="test",
                message_id="msg-1",
            )
        )
        coordinator.observe(
            AssistantMessage(
                content=[TextBlock(text="重复消息")],
                model="test",
                message_id="msg-1",
            )
        )
        await coordinator.drain(1)
        coordinator.close()
        return emitted, service

    emitted, service = asyncio.run(scenario())

    assert [call["source_kind"] for call in service.calls] == [
        "thinking",
        "assistant_response",
    ]
    assert service.calls[0]["source_text"] == "核对告警并关联证据"
    assert service.calls[0]["block_index"] == 2
    assert service.calls[1]["source_text"] == "第一段答案\n第二段答案"
    payloads = [event.data["payload"] for event in emitted]
    assert payloads[0]["summary_id"] == "speech:msg-1:thinking:2"
    assert payloads[0]["block_index"] == 2
    assert payloads[1]["summary_id"] == "speech:msg-1:assistant_response"
    assert "block_index" not in payloads[1]
    assert all(payload["scope"] == "main" for payload in payloads)


def test_coordinator_skips_disabled_empty_tool_only_and_subagent_content() -> None:
    async def scenario() -> tuple[list[AgentGovControlEvent], _RecordingService]:
        emitted: list[AgentGovControlEvent] = []
        service = _RecordingService()

        async def emit(event: AgentGovControlEvent) -> None:
            emitted.append(event)

        coordinator = SpeechSummaryCoordinator(
            service=service,  # type: ignore[arg-type]
            run_id="run-1",
            boundaries=(),
            enabled=True,
            emit=emit,
        )
        coordinator.observe(
            AssistantMessage(
                content=[TextBlock(text="不会触发")],
                model="test",
                message_id="msg-disabled",
            )
        )
        request_disabled = SpeechSummaryCoordinator(
            service=service,  # type: ignore[arg-type]
            run_id="run-request-disabled",
            boundaries=(
                "thinking_block_completed",
                "assistant_response_completed",
            ),
            enabled=False,
            emit=emit,
        )
        request_disabled.observe(
            AssistantMessage(
                content=[TextBlock(text="请求未显式开启时不会触发")],
                model="test",
                message_id="msg-request-disabled",
            )
        )

        enabled = SpeechSummaryCoordinator(
            service=service,  # type: ignore[arg-type]
            run_id="run-2",
            boundaries=(
                "thinking_block_completed",
                "assistant_response_completed",
            ),
            enabled=True,
            emit=emit,
        )
        enabled.observe(
            AssistantMessage(
                content=[TextBlock(text="子 Agent 文本")],
                model="test",
                message_id="msg-sub",
                parent_tool_use_id="tool-parent",
            )
        )
        enabled.observe(
            AssistantMessage(
                content=[TextBlock(text="   ")],
                model="test",
                message_id="msg-empty",
            )
        )
        enabled.observe(
            AssistantMessage(
                content=[ToolUseBlock(id="tool-1", name="Read", input={})],
                model="test",
                message_id="msg-tool",
            )
        )
        enabled.observe(
            _stream_event(
                {
                    "type": "message_start",
                    "message": {"id": "msg-sub-thinking"},
                },
                parent_tool_use_id="tool-parent",
            )
        )
        await enabled.drain(0.1)
        return emitted, service

    emitted, service = asyncio.run(scenario())

    assert emitted == []
    assert service.calls == []


def test_final_assistant_cancels_stale_thinking_summary() -> None:
    class _BlockingService(_RecordingService):
        def __init__(self) -> None:
            super().__init__()
            self.thinking_started = asyncio.Event()
            self.thinking_cancelled = asyncio.Event()

        async def generate(self, **kwargs: object) -> SpeechSummaryOutput:
            self.calls.append(kwargs)
            if kwargs["source_kind"] == "thinking":
                self.thinking_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.thinking_cancelled.set()
                    raise
            return SpeechSummaryOutput(text="已完成关键证据核对并形成可用结论")

    async def scenario() -> tuple[list[AgentGovControlEvent], _BlockingService]:
        emitted: list[AgentGovControlEvent] = []
        service = _BlockingService()

        async def emit(event: AgentGovControlEvent) -> None:
            emitted.append(event)

        coordinator = SpeechSummaryCoordinator(
            service=service,  # type: ignore[arg-type]
            run_id="run-1",
            boundaries=(
                "thinking_block_completed",
                "assistant_response_completed",
            ),
            enabled=True,
            emit=emit,
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "message_start",
                    "message": {"id": "msg-1"},
                }
            )
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "仍在分析",
                    },
                }
            )
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "content_block_stop",
                    "index": 0,
                }
            )
        )
        await service.thinking_started.wait()
        coordinator.observe(
            AssistantMessage(
                content=[TextBlock(text="最终回答")],
                model="test",
                message_id="msg-1",
            )
        )
        await coordinator.drain(1)
        coordinator.close()
        return emitted, service

    emitted, service = asyncio.run(scenario())

    assert service.thinking_cancelled.is_set()
    assert [event.data["payload"]["source_kind"] for event in emitted] == ["assistant_response"]


def test_tool_only_assistant_does_not_cancel_pending_thinking_summary() -> None:
    class _BlockingService(_RecordingService):
        def __init__(self) -> None:
            super().__init__()
            self.thinking_started = asyncio.Event()
            self.release_thinking = asyncio.Event()

        async def generate(self, **kwargs: object) -> SpeechSummaryOutput:
            self.calls.append(kwargs)
            self.thinking_started.set()
            await self.release_thinking.wait()
            return SpeechSummaryOutput(text="正在等待工具结果并继续核对关键证据")

    async def scenario() -> tuple[list[AgentGovControlEvent], _BlockingService]:
        emitted: list[AgentGovControlEvent] = []
        service = _BlockingService()

        async def emit(event: AgentGovControlEvent) -> None:
            emitted.append(event)

        coordinator = SpeechSummaryCoordinator(
            service=service,  # type: ignore[arg-type]
            run_id="run-1",
            boundaries=(
                "thinking_block_completed",
                "assistant_response_completed",
            ),
            enabled=True,
            emit=emit,
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "message_start",
                    "message": {"id": "msg-1"},
                }
            )
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "正在等待工具结果",
                    },
                }
            )
        )
        coordinator.observe(
            _stream_event(
                {
                    "type": "content_block_stop",
                    "index": 0,
                }
            )
        )
        await service.thinking_started.wait()
        coordinator.observe(
            AssistantMessage(
                content=[ToolUseBlock(id="tool-1", name="Read", input={})],
                model="test",
                message_id="msg-1",
            )
        )
        service.release_thinking.set()
        await coordinator.drain(1)
        coordinator.close()
        return emitted, service

    emitted, service = asyncio.run(scenario())

    assert [call["source_kind"] for call in service.calls] == ["thinking"]
    assert [event.data["payload"]["source_kind"] for event in emitted] == ["thinking"]


def _speech_control() -> AgentGovControlEvent:
    text = "已完成关键证据核对并形成可用结论"
    return AgentGovControlEvent(
        name="speech_summary",
        data={
            "run_id": "run-1",
            "payload": {
                "summary_id": "speech:msg-1:assistant_response",
                "source_kind": "assistant_response",
                "message_id": "msg-1",
                "scope": "main",
                "text": text,
                "char_count": len(text),
            },
        },
    )


class _RouteRuntime:
    def __init__(self) -> None:
        self.flags: list[bool] = []

    async def stream_events(
        self,
        _req: object,
        *,
        profile: object,
        with_speech_summary: bool = False,
    ):
        self.flags.append(with_speech_summary)
        yield AgentGovControlEvent(
            name="session",
            data={"run_id": "run-1", "session_id": "session-1"},
        )
        yield AgentGovHeartbeatEvent(run_id="run-1", timestamp="now")
        if with_speech_summary:
            yield _speech_control()
        yield AgentGovControlEvent(name="done", data={})


def _event_names(text: str) -> list[str]:
    return [line.removeprefix("event: ") for line in text.splitlines() if line.startswith("event: ")]


def _event_data(text: str, event_name: str) -> dict[str, object]:
    blocks = text.split("\n\n")
    block = next(item for item in blocks if f"event: {event_name}\n" in item)
    data_line = next(line for line in block.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def test_sdk_sse_projects_canonical_summary_and_keeps_done_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RouteRuntime()
    profile = SimpleNamespace(requires_web_hitl=False)
    monkeypatch.setattr(
        "app.routers.claude_sdk_events.resolve_business_profile",
        lambda *_args: profile,
    )
    app = FastAPI()
    app.include_router(
        create_claude_sdk_events_router(
            runtime=runtime,  # type: ignore[arg-type]
            settings=AppSettings(_env_file=None),
            agent_registry_store=object(),  # type: ignore[arg-type]
            require_api_key=lambda: None,
        )
    )

    with TestClient(app) as client:
        enabled = client.post(
            "/api/agent-runtime/sdk-events",
            json={
                "message": "hello",
                "agent_id": "agent-1",
                "with_speech_summary": True,
            },
        )
        disabled = client.post(
            "/api/agent-runtime/sdk-events",
            json={"message": "hello", "agent_id": "agent-1"},
        )

    assert enabled.status_code == 200
    assert _event_names(enabled.text)[-1] == "agentgov.done"
    assert ": keepalive" in enabled.text
    envelope = _event_data(enabled.text, "agentgov.speech_summary")
    assert envelope["v"] == 1
    assert envelope["type"] == "agentgov.speech_summary"
    assert envelope["run_id"] == "run-1"
    assert envelope["seq"] == 2
    assert envelope["payload"]["source_kind"] == "assistant_response"
    assert "agentgov.speech_summary" not in disabled.text
    assert runtime.flags == [True, False]


@pytest.mark.parametrize("event_mode", ["raw", "semantic"])
def test_chat_stream_projects_same_canonical_summary_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    event_mode: str,
) -> None:
    runtime = _RouteRuntime()
    profile = SimpleNamespace(requires_web_hitl=False)
    monkeypatch.setattr(
        "app.routers.chat.resolve_business_profile",
        lambda *_args: profile,
    )
    app = FastAPI()
    app.include_router(
        create_chat_router(
            runtime=runtime,  # type: ignore[arg-type]
            settings=AppSettings(_env_file=None),
            agent_registry_store=object(),  # type: ignore[arg-type]
            require_api_key=lambda: None,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/chat/stream?event_mode={event_mode}",
            json={
                "message": "hello",
                "agent_id": "agent-1",
                "with_speech_summary": True,
            },
        )

    assert response.status_code == 200
    names = _event_names(response.text)
    assert names[-1] == "done"
    assert names.count("heartbeat") == 1
    assert names.count("agentgov.speech_summary") == 1
    assert ": keepalive" not in response.text
    assert 'event: message\ndata: {"type": "agentgov.speech_summary"' not in response.text
    envelope = _event_data(response.text, "agentgov.speech_summary")
    assert envelope["seq"] == 3
    assert envelope["payload"]["summary_id"] == "speech:msg-1:assistant_response"
    assert envelope["payload"]["char_count"] == len(envelope["payload"]["text"])
