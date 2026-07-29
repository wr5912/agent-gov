from __future__ import annotations

from dataclasses import dataclass

import pytest
from app.routers.claude_sdk_events import create_claude_sdk_events_router
from app.runtime.chat_stream_projector import ChatStreamProjector
from app.runtime.managed_claude_events import (
    AgentGovControlEvent,
    ClaudeSdkMessageEvent,
    sdk_message_to_json,
)
from app.runtime.settings import AppSettings
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class FutureSdkMessage:
    value: str


@dataclass
class UnsupportedSdkMessage:
    value: object


class _FakeRuntime:
    async def stream_events(self, req, *, profile, **kwargs):
        from claude_agent_sdk import AssistantMessage, StreamEvent, ThinkingBlock

        yield AgentGovControlEvent(
            name="session",
            data={"run_id": "run-native", "session_id": "session-native"},
        )
        yield ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="stream-1",
                session_id="sdk-session",
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "逐步分析"},
                },
            )
        )
        yield ClaudeSdkMessageEvent(
            AssistantMessage(
                content=[ThinkingBlock(thinking="逐步分析", signature="signed-thinking")],
                model="test-model",
            )
        )
        yield ClaudeSdkMessageEvent(FutureSdkMessage(value="future"))
        yield AgentGovControlEvent(name="result", data={"run_id": "run-native", "errors": []})
        yield AgentGovControlEvent(name="done", data={})


def test_native_sdk_route_preserves_one_frame_per_sdk_yield(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.routers.claude_sdk_events.resolve_business_profile",
        lambda settings, store, agent_id: type("Profile", (), {"requires_web_hitl": False})(),
    )
    app = FastAPI()
    app.include_router(
        create_claude_sdk_events_router(
            runtime=_FakeRuntime(),  # type: ignore[arg-type]
            settings=AppSettings(),
            agent_registry_store=object(),  # type: ignore[arg-type]
            require_api_key=lambda: None,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/agent-runtime/sdk-events",
            json={"message": "hello", "agent_id": "business-agent"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-agentgov-run-id"] == "run-native"
    assert response.headers["x-agentgov-session-id"] == "session-native"
    event_names = [line.removeprefix("event: ") for line in response.text.splitlines() if line.startswith("event: ")]
    assert event_names == [
        "agentgov.session",
        "claude.sdk.StreamEvent",
        "claude.sdk.AssistantMessage",
        "claude.sdk.FutureSdkMessage",
        "agentgov.result",
        "agentgov.done",
    ]
    assert response.text.count("event: claude.sdk.") == 3
    assert '"thinking":"逐步分析"' in response.text.replace(" ", "")
    assert '"signature":"signed-thinking"' in response.text.replace(" ", "")


def test_native_sdk_route_requires_agent_id(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(
        create_claude_sdk_events_router(
            runtime=_FakeRuntime(),  # type: ignore[arg-type]
            settings=AppSettings(),
            agent_registry_store=object(),  # type: ignore[arg-type]
            require_api_key=lambda: None,
        )
    )
    with TestClient(app) as client:
        response = client.post("/api/agent-runtime/sdk-events", json={"message": "hello"})
    assert response.status_code == 422


def test_native_sdk_codec_fails_instead_of_stringifying_unknown_values() -> None:
    with pytest.raises(TypeError, match="Unsupported Claude SDK value"):
        sdk_message_to_json(UnsupportedSdkMessage(value=object()))


def test_chat_projector_streams_real_thinking_and_marks_token_counter_as_metric() -> None:
    from claude_agent_sdk import StreamEvent, SystemMessage

    projector = ChatStreamProjector()
    thinking = projector.project(
        ClaudeSdkMessageEvent(
            StreamEvent(
                uuid="thinking-delta",
                session_id="sdk-session",
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "真实推理增量"},
                },
            )
        )
    )[0]
    counter = projector.project(
        ClaudeSdkMessageEvent(
            SystemMessage(
                subtype="thinking_tokens",
                data={"estimated_tokens": 42, "estimated_tokens_delta": 1},
            )
        )
    )[0]

    assert thinking["data"]["event"] == "StreamEvent:thinking_delta"
    assert thinking["data"]["text"] == "真实推理增量"
    assert thinking["data"]["text_kind"] == "delta"
    assert counter["data"]["event"] == "SystemMessage:thinking_tokens"
    assert counter["data"]["text"] == ""
    assert counter["data"]["text_kind"] == "metric"
