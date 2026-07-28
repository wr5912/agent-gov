from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import (
    ChatRequest,
    ChatResponse,
    OpenAIChatCompletionRequest,
)
from app.runtime.stream_request_schemas import ChatStreamRequest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_test_utils import load_test_app


@pytest.mark.parametrize(
    "payload",
    [
        {"messages": []},
        {"messages": [{"role": "tool", "content": "hello"}]},
        {"messages": [{"role": "user", "content": "   "}]},
        {
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
        {
            "messages": [{"role": "user", "content": "hello"}],
            "with_speech_summary": True,
        },
    ],
)
def test_chat_completions_request_rejects_unsupported_or_empty_input(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OpenAIChatCompletionRequest.model_validate(payload)


def test_speech_flag_is_owned_only_by_chat_stream_request() -> None:
    stream = ChatStreamRequest.model_validate(
        {
            "message": "hello",
            "agent_id": "agent-1",
            "with_speech_summary": True,
        }
    )
    assert stream.with_speech_summary is True

    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {
                "message": "hello",
                "agent_id": "agent-1",
                "with_speech_summary": True,
            }
        )
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "   ", "agent_id": "agent-1"})


def test_legacy_routes_are_deprecated_and_runtime_failure_is_not_fake_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = load_test_app(
        monkeypatch,
        tmp_path,
        requires_web_hitl=False,
    )

    results = iter(
        (
            ChatResponse(
                run_id="run-failed",
                session_id="session-failed",
                answer="partial answer",
                errors=["runtime failed"],
            ),
            ChatResponse(
                run_id="run-empty",
                session_id="session-empty",
                answer="",
                errors=[],
            ),
        )
    )

    async def failed_run(_req, *, profile=None, **_kwargs):
        return next(results)

    monkeypatch.setattr(module.runtime, "run", failed_run)
    with TestClient(module.app) as client:
        failed = client.post(
            "/v1/chat/completions",
            json={
                "model": "requested-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        empty = client.post(
            "/v1/chat/completions",
            json={
                "model": "requested-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        openapi = client.get("/openapi.json").json()

    assert failed.status_code == 502
    assert failed.json() == {
        "error": {
            "message": "Agent runtime failed to produce a chat completion.",
            "type": "server_error",
            "code": "agent_runtime_error",
        }
    }
    assert empty.status_code == 502
    assert empty.json() == failed.json()
    for path in (
        "/api/chat",
        "/api/chat/stream",
        "/v1/chat/completions",
    ):
        assert openapi["paths"][path]["post"]["deprecated"] is True
    error_schema = openapi["paths"]["/v1/chat/completions"]["post"]["responses"]["502"]["content"]["application/json"]["schema"]
    assert error_schema["$ref"].endswith("/OpenAIErrorResponse")


def test_hitl_incompatible_surfaces_fail_before_starting_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = load_test_app(
        monkeypatch,
        tmp_path,
        api_key="test-api-key",
        raw_events_enabled=True,
        requires_web_hitl=True,
    )

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("HITL preflight must reject before runtime execution")

    monkeypatch.setattr(module.runtime, "run", must_not_run)
    monkeypatch.setattr(module.runtime, "stream_events", must_not_run)
    monkeypatch.setattr(module.runtime_raw_events_backend, "start", must_not_run)
    headers = {"Authorization": "Bearer test-api-key"}
    agent_id = DEFAULT_BUSINESS_AGENT_ID

    with TestClient(module.app) as client:
        chat = client.post(
            "/api/chat",
            headers=headers,
            json={"message": "hello", "agent_id": agent_id},
        )
        completions = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        strict_stream = client.post(
            "/v1/responses",
            headers=headers,
            json={"input": "hello", "stream": True},
        )
        control_non_stream = client.post(
            "/v1/responses",
            headers=headers,
            json={"input": "hello", "agentgov": {"agent_id": agent_id}},
        )
        chat_stream = client.post(
            "/api/chat/stream",
            headers=headers,
            json={"message": "hello", "agent_id": agent_id},
        )
        sdk_stream = client.post(
            "/api/agent-runtime/sdk-events",
            headers=headers,
            json={"message": "hello", "agent_id": agent_id},
        )
        control_stream = client.post(
            "/v1/responses",
            headers=headers,
            json={
                "input": "hello",
                "stream": True,
                "agentgov": {"agent_id": agent_id},
            },
        )
        raw_non_stream = client.post(
            "/api/debug/agent-runtime/raw-events",
            headers=headers,
            json={"message": "hello", "agent_id": agent_id},
        )
        raw_stream = client.post(
            "/api/debug/agent-runtime/raw-events",
            headers=headers,
            json={
                "message": "hello",
                "agent_id": agent_id,
                "stream": True,
            },
        )

    assert {
        chat.status_code,
        completions.status_code,
        strict_stream.status_code,
        control_non_stream.status_code,
        raw_non_stream.status_code,
    } == {422}
    assert {
        chat_stream.status_code,
        sdk_stream.status_code,
        control_stream.status_code,
        raw_stream.status_code,
    } == {503}


def test_non_speech_surfaces_reject_explicit_speech_field(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = load_test_app(
        monkeypatch,
        tmp_path,
        api_key="test-api-key",
        raw_events_enabled=True,
        requires_web_hitl=False,
    )
    headers = {"Authorization": "Bearer test-api-key"}
    agent_id = DEFAULT_BUSINESS_AGENT_ID

    with TestClient(module.app) as client:
        responses = [
            client.post(
                "/api/chat",
                headers=headers,
                json={
                    "message": "hello",
                    "agent_id": agent_id,
                    "with_speech_summary": True,
                },
            ),
            client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "with_speech_summary": True,
                },
            ),
            client.post(
                "/api/debug/agent-runtime/raw-events",
                headers=headers,
                json={
                    "message": "hello",
                    "agent_id": agent_id,
                    "with_speech_summary": True,
                },
            ),
        ]

    assert [response.status_code for response in responses] == [422, 422, 422]
