from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.runtime.managed_claude_events import AgentGovControlEvent
from app.runtime.openai_responses_stream import iter_responses_sse
from fastapi.testclient import TestClient

from app_test_utils import load_test_app
from test_agent_workspace_packages import _import_new_agent


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


async def _source(*events: AgentGovControlEvent):
    for event in events:
        yield event


def _collect(*, control: bool) -> str:
    events = (
        AgentGovControlEvent(
            name="session",
            data={
                "run_id": "run-1",
                "session_id": "session-1",
            },
        ),
        _speech_control(),
        AgentGovControlEvent(
            name="result",
            data={
                "run_id": "run-1",
                "session_id": "session-1",
                "errors": [],
            },
        ),
        AgentGovControlEvent(name="done", data={}),
    )

    async def scenario() -> str:
        chunks = [
            chunk
            async for chunk in iter_responses_sse(
                _source(*events),
                model="test-model",
                effective_agent_id="agent-1",
                control=control,
            )
        ]
        return "".join(chunks)

    return asyncio.run(scenario())


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


def test_control_responses_projects_canonical_summary_before_standard_terminal() -> None:
    events = _events(_collect(control=True))
    names = [name for name, _ in events]

    assert names[-2:] == ["agentgov.done", "response.completed"]
    assert names.count("agentgov.speech_summary") == 1
    envelope = dict(events)["agentgov.speech_summary"]
    assert envelope["v"] == 1
    assert envelope["type"] == "agentgov.speech_summary"
    assert envelope["run_id"] == "run-1"
    assert envelope["seq"] == names.index("agentgov.speech_summary") + 1
    payload = envelope["payload"]
    assert payload["summary_id"] == "speech:msg-1:assistant_response"
    assert "block_index" not in payload


def test_strict_responses_drops_all_agentgov_speech_events() -> None:
    events = _events(_collect(control=False))
    names = [name for name, _ in events]

    assert all(not name.startswith("agentgov.") for name in names)
    assert names[-1] == "response.completed"
    assert "agentgov" not in dict(events)["response.completed"]["response"]


def test_responses_route_accepts_speech_only_for_control_streaming(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = load_test_app(
        monkeypatch,
        tmp_path,
        requires_web_hitl=False,
    )
    seen_flags: list[bool] = []

    async def fake_stream(
        _req,
        *,
        profile=None,
        with_speech_summary: bool = False,
    ):
        seen_flags.append(with_speech_summary)
        yield AgentGovControlEvent(
            name="session",
            data={"run_id": "run-1", "session_id": "session-1"},
        )
        if with_speech_summary:
            yield _speech_control()
        yield AgentGovControlEvent(
            name="result",
            data={
                "run_id": "run-1",
                "session_id": "session-1",
                "errors": [],
            },
        )
        yield AgentGovControlEvent(name="done", data={})

    monkeypatch.setattr(module.runtime, "stream_events", fake_stream)
    with TestClient(module.app) as client:
        assert (
            _import_new_agent(
                client,
                agent_id="speech-agent",
                name="Speech Agent",
                requires_web_hitl=False,
            ).status_code
            == 200
        )
        enabled = client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "stream": True,
                "agentgov": {
                    "agent_id": "speech-agent",
                    "with_speech_summary": True,
                },
            },
        )
        non_stream = client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "agentgov": {
                    "agent_id": "speech-agent",
                    "with_speech_summary": True,
                },
            },
        )
        strict_unknown = client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "stream": True,
                "with_speech_summary": True,
            },
        )

    assert enabled.status_code == 200
    assert "event: agentgov.speech_summary" in enabled.text
    assert _events(enabled.text)[-1][0] == "response.completed"
    assert seen_flags == [True]
    assert non_stream.status_code == 422
    assert "requires stream=true" in non_stream.text
    assert strict_unknown.status_code == 422
