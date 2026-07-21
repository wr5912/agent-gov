from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.runtime import claude_prompt_suggestions
from app.runtime.async_iterators import close_async_iterator
from app.runtime.message_utils import extract_assistant_text_snapshot, extract_stream_text_delta
from app.runtime.openai_responses_stream import iter_responses_sse
from app.runtime.schemas import ChatRequest

from app_test_utils import load_test_app

_SESSION = {
    "event": "session",
    "data": {"run_id": "run-stream", "session_id": "session-stream", "sdk_session_id": "sdk-stream"},
}
_RESULT = {
    "event": "result",
    "data": {
        "run_id": "run-stream",
        "session_id": "session-stream",
        "sdk_session_id": "sdk-stream",
        "errors": [],
        "agent_activity": {},
    },
}
_DONE = {"event": "done", "data": "[DONE]"}


def _patch_sdk_query(monkeypatch, fake_query) -> None:
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

        async def query(self, prompt, session_id="default"):
            self.responses = fake_query(prompt=prompt, options=self.options)

        async def receive_response(self):
            assert self.responses is not None
            async for message in self.responses:
                yield message

        async def disconnect(self):
            if self.responses is not None:
                await close_async_iterator(self.responses)
            if self.control_task is not None:
                await asyncio.wait_for(self.control_task, timeout=1)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(claude_prompt_suggestions, "query_with_prompt_suggestions", fake_query)
    monkeypatch.setattr(claude_prompt_suggestions, "PromptSuggestionClaudeClient", FakeClaudeSDKClient)


async def _frames(values):
    for value in values:
        yield value


def _project(values) -> list[tuple[str, object]]:
    async def collect() -> str:
        chunks: list[str] = []
        async for chunk in iter_responses_sse(
            _frames(values),
            model="test-model",
            effective_agent_id="test-agent",
            control=True,
        ):
            chunks.append(chunk)
        return "".join(chunks)

    events: list[tuple[str, object]] = []
    for block in asyncio.run(collect()).split("\n\n"):
        name: str | None = None
        data: object = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if name is not None:
            events.append((name, data))
    return events


def _message(text: str, *, kind: str, event: str) -> dict:
    return {
        "event": "message",
        "data": {"event": event, "text": text, "text_kind": kind, "raw": {}},
    }


def test_stream_event_text_delta_preserves_whitespace_and_rejects_other_events() -> None:
    from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock

    delta = StreamEvent(
        uuid="event-1",
        session_id="session-1",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": " 你好\n"}},
    )
    thinking = StreamEvent(
        uuid="event-2",
        session_id="session-1",
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "secret"}},
    )

    assert extract_stream_text_delta(delta) == " 你好\n"
    assert extract_stream_text_delta(thinking) is None
    assert extract_stream_text_delta({"event": delta.event}) is None
    assert extract_assistant_text_snapshot(AssistantMessage(content=[TextBlock(text=" 你好"), TextBlock(text="\n")], model="test")) == " 你好\n"


def test_delta_and_matching_snapshot_emit_exact_text_without_duplicate() -> None:
    events = _project(
        [
            _SESSION,
            _message("你", kind="delta", event="StreamEvent"),
            _message("好", kind="delta", event="StreamEvent"),
            _message("你好", kind="snapshot", event="AssistantMessage"),
            _RESULT,
            _DONE,
        ]
    )

    deltas = [data["delta"] for name, data in events if name == "response.output_text.delta"]  # type: ignore[index]
    completed = next(data for name, data in events if name == "response.completed")
    assert "".join(deltas) == "你好"
    assert completed["response"]["output"][0]["content"][0]["text"] == "你好"  # type: ignore[index]


def test_snapshot_suffix_is_emitted_when_last_delta_was_not_observed() -> None:
    events = _project(
        [
            _SESSION,
            _message("你", kind="delta", event="StreamEvent"),
            _message("你好", kind="snapshot", event="AssistantMessage"),
            _RESULT,
            _DONE,
        ]
    )

    deltas = [data["delta"] for name, data in events if name == "response.output_text.delta"]  # type: ignore[index]
    assert deltas == ["你", "好"]


def test_divergent_snapshot_fails_closed_with_stable_error_code() -> None:
    events = _project(
        [
            _SESSION,
            _message("你", kind="delta", event="StreamEvent"),
            _message("您好", kind="snapshot", event="AssistantMessage"),
            _RESULT,
            _DONE,
        ]
    )
    names = [name for name, _ in events]
    failed = next(data for name, data in events if name == "response.failed")

    assert "response.completed" not in names
    assert names.count("response.failed") == 1
    assert failed["error"]["error_code"] == "STREAM_TEXT_DIVERGED"  # type: ignore[index]


def test_runtime_partial_events_are_transport_only_and_timings_reach_trace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

    captured_options = []

    async def fake_query(*, prompt, options, transport=None):
        captured_options.append(options)
        async for _ in prompt:
            pass
        session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": session_id},
            [{"type": "user", "uuid": "stream-entry"}],
        )
        yield StreamEvent(
            uuid="stream-thinking",
            session_id=session_id,
            event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hidden"}},
        )
        for index, text in enumerate(("你", "好"), start=1):
            yield StreamEvent(
                uuid=f"stream-{index}",
                session_id=session_id,
                event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
            )
        yield AssistantMessage(content=[TextBlock(text="你好")], model="test-model", session_id=session_id)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=session_id,
            result="你好",
        )

    monkeypatch.setenv("INCLUDE_PARTIAL_MESSAGES", "true")
    module = load_test_app(monkeypatch, tmp_path)
    _patch_sdk_query(monkeypatch, fake_query)
    monkeypatch.setattr(module.runtime.model_provider_router, "ensure_agent_runtime_ready", lambda: None)
    trace_updates: list[dict] = []
    monkeypatch.setattr(module.runtime.langfuse, "upsert_trace", lambda trace_id, **kwargs: trace_updates.append(kwargs))

    async def collect() -> list[dict]:
        return [event async for event in module.runtime.stream(ChatRequest(message="你好", session_id="partial-stream"))]

    frames = asyncio.run(collect())
    stream_messages = [frame for frame in frames if frame.get("event") == "message"]
    run_id = next(frame["data"]["run_id"] for frame in frames if frame.get("event") == "session")
    record = module.feedback_store.find_run(run_id=run_id)

    assert captured_options[0].include_partial_messages is True
    assert [frame["data"].get("text_kind") for frame in stream_messages] == ["delta", "delta", "snapshot", "snapshot"]
    assert record is not None
    assert all(message.get("event") != "StreamEvent" for message in record["messages"])
    assert record["answer_summary"] == "你好"
    assert trace_updates
    timing = trace_updates[-1]["metadata"]
    assert {"provider_gate_ms", "sdk_init_ms", "first_text_delta_ms", "complete_ms"} <= timing.keys()


def test_runtime_divergence_never_emits_result_or_completes_turn(monkeypatch, tmp_path: Path) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        session_id = options.resume or options.session_id
        yield StreamEvent(
            uuid="stream-diverged",
            session_id=session_id,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你"}},
        )
        yield AssistantMessage(content=[TextBlock(text="您好")], model="test-model", session_id=session_id)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=session_id,
            result="您好",
        )

    monkeypatch.setenv("INCLUDE_PARTIAL_MESSAGES", "true")
    module = load_test_app(monkeypatch, tmp_path)
    _patch_sdk_query(monkeypatch, fake_query)
    monkeypatch.setattr(module.runtime.model_provider_router, "ensure_agent_runtime_ready", lambda: None)

    async def collect() -> list[dict]:
        return [event async for event in module.runtime.stream(ChatRequest(message="你好", session_id="diverged-stream"))]

    frames = asyncio.run(collect())
    error = next(frame for frame in frames if frame.get("event") == "error")
    session = module.session_store.get("diverged-stream")

    assert error["data"]["error_code"] == "STREAM_TEXT_DIVERGED"
    assert not any(frame.get("event") == "result" for frame in frames)
    assert session is not None and session.turns == 0 and session.active_run_id is None
