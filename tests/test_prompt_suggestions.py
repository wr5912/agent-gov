from __future__ import annotations

import asyncio
from typing import Any

import pytest
from app.runtime import claude_prompt_suggestions as prompt_suggestions
from app.runtime.claude_prompt_suggestions import (
    PromptSuggestionClaudeClient,
    PromptSuggestionMessage,
    query_with_prompt_suggestions,
)
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.stores.feedback_store import FeedbackStore
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from test_claude_runtime import _settings


class _FakeRawQuery:
    def __init__(self, raw_messages: list[dict[str, Any]]) -> None:
        self.raw_messages = raw_messages

    async def receive_messages(self):
        for message in self.raw_messages:
            yield message


class _FakeClient:
    raw_messages: list[dict[str, Any]] = []
    instances: list[_FakeClient] = []

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self._query = _FakeRawQuery(self.raw_messages)
        self.connected_prompt: object = None
        self.disconnected = False
        self.instances.append(self)

    async def connect(self, prompt) -> None:
        self.connected_prompt = prompt

    async def disconnect(self) -> None:
        self.disconnected = True


async def _supported(_options: ClaudeAgentOptions) -> bool:
    return True


def _result_raw(session_id: str = "sdk-session") -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": session_id,
        "result": "answer",
    }


def test_adapter_consumes_through_eof_and_preserves_official_messages(monkeypatch) -> None:
    _FakeClient.instances = []
    _FakeClient.raw_messages = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "answer"}], "model": "test-model", "usage": {}},
            "session_id": "sdk-session",
            "uuid": "assistant-1",
        },
        _result_raw(),
        {
            "type": "prompt_suggestion",
            "suggestion": "  请继续检查失败路径。  ",
            "uuid": "suggestion-1",
            "session_id": "sdk-session",
        },
    ]
    monkeypatch.setattr(prompt_suggestions, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(prompt_suggestions, "_prompt_suggestions_supported", _supported)

    async def collect():
        return [
            item
            async for item in query_with_prompt_suggestions(
                prompt="hello",
                options=ClaudeAgentOptions(extra_args={"existing-flag": "kept"}),
            )
        ]

    messages = asyncio.run(collect())

    assert [type(message).__name__ for message in messages] == ["AssistantMessage", "ResultMessage", "PromptSuggestionMessage"]
    assert messages[-1] == PromptSuggestionMessage("请继续检查失败路径。", "suggestion-1", "sdk-session")
    client = _FakeClient.instances[0]
    assert client.options.extra_args == {"prompt-suggestions": None, "existing-flag": "kept"}
    assert client.connected_prompt == "hello"
    assert client.disconnected is True


def test_adapter_skips_malformed_suggestions_and_preserves_explicit_false(monkeypatch, caplog) -> None:
    _FakeClient.instances = []
    _FakeClient.raw_messages = [
        {"type": "prompt_suggestion", "suggestion": "   "},
        {"type": "prompt_suggestion", "suggestion": {"hostile": True}},
    ]
    monkeypatch.setattr(prompt_suggestions, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(prompt_suggestions, "_prompt_suggestions_supported", _supported)

    async def collect():
        return [
            item
            async for item in query_with_prompt_suggestions(
                prompt="hello",
                options=ClaudeAgentOptions(extra_args={"prompt-suggestions": "false"}),
            )
        ]

    assert asyncio.run(collect()) == []
    assert _FakeClient.instances[0].options.extra_args["prompt-suggestions"] == "false"
    assert _FakeClient.instances[0].disconnected is True
    assert caplog.text.count("Skipping malformed Claude prompt_suggestion message") == 2


@pytest.mark.parametrize(
    ("raw_messages", "expected_types"),
    [
        ([_result_raw()], ["ResultMessage"]),
        (
            [
                {"type": "prompt_suggestion", "suggestion": "same", "uuid": "one", "session_id": "sdk"},
                _result_raw(),
                {"type": "prompt_suggestion", "suggestion": "same", "uuid": "two", "session_id": "sdk"},
            ],
            ["PromptSuggestionMessage", "ResultMessage"],
        ),
    ],
)
def test_adapter_accepts_absent_repeated_and_differently_ordered_suggestions(monkeypatch, raw_messages, expected_types) -> None:
    _FakeClient.instances = []
    _FakeClient.raw_messages = raw_messages
    monkeypatch.setattr(prompt_suggestions, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(prompt_suggestions, "_prompt_suggestions_supported", _supported)

    async def collect():
        return [item async for item in query_with_prompt_suggestions(prompt="hello", options=ClaudeAgentOptions())]

    assert [type(message).__name__ for message in asyncio.run(collect())] == expected_types
    assert _FakeClient.instances[0].disconnected is True


def test_interactive_client_reads_suggestion_after_result() -> None:
    client = PromptSuggestionClaudeClient(ClaudeAgentOptions())
    client._prompt_suggestions_enabled = True
    client._query = _FakeRawQuery(
        [
            _result_raw(),
            {
                "type": "prompt_suggestion",
                "suggestion": "继续验证交互路径",
                "uuid": "suggestion-interactive",
                "session_id": "sdk-session",
            },
        ]
    )

    async def collect():
        return [message async for message in client.receive_response()]

    messages = asyncio.run(collect())

    assert [type(message).__name__ for message in messages] == ["ResultMessage", "PromptSuggestionMessage"]


def test_interactive_trailing_timeout_does_not_fail_completed_result() -> None:
    class BlockingAfterResultQuery:
        async def receive_messages(self):
            yield _result_raw()
            await asyncio.Event().wait()

    client = PromptSuggestionClaudeClient(ClaudeAgentOptions())
    client._prompt_suggestions_enabled = True
    client._query = BlockingAfterResultQuery()

    async def collect():
        return [
            message
            async for message in prompt_suggestions._receive_messages_with_trailing_suggestion(
                client,
                trailing_timeout_seconds=0.01,
            )
        ]

    messages = asyncio.run(asyncio.wait_for(collect(), timeout=1))

    assert [type(message).__name__ for message in messages] == ["ResultMessage"]


def test_adapter_falls_back_once_before_execution_and_removes_unsupported_flag(monkeypatch) -> None:
    calls: list[ClaudeAgentOptions] = []

    async def unsupported(_options: ClaudeAgentOptions) -> bool:
        return False

    async def fake_query(*, prompt, options, transport=None):
        calls.append(options)
        assert prompt == "hello"
        yield AssistantMessage(content=[TextBlock(text="fallback")], model="test-model")

    monkeypatch.setattr(prompt_suggestions, "_prompt_suggestions_supported", unsupported)
    monkeypatch.setattr(prompt_suggestions.claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(prompt_suggestions, "ClaudeSDKClient", lambda **_kwargs: pytest.fail("client must not start on fallback"))

    async def collect():
        return [
            item
            async for item in query_with_prompt_suggestions(
                prompt="hello",
                options=ClaudeAgentOptions(extra_args={"prompt-suggestions": "false", "keep": "yes"}),
            )
        ]

    messages = asyncio.run(collect())

    assert len(messages) == 1
    assert len(calls) == 1
    assert calls[0].extra_args == {"keep": "yes"}


def test_adapter_disconnects_when_official_parser_rejects_a_message(monkeypatch) -> None:
    _FakeClient.instances = []
    _FakeClient.raw_messages = [{"type": "assistant"}]
    monkeypatch.setattr(prompt_suggestions, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(prompt_suggestions, "_prompt_suggestions_supported", _supported)

    async def collect():
        return [item async for item in query_with_prompt_suggestions(prompt="hello", options=ClaudeAgentOptions())]

    with pytest.raises(Exception, match="Missing required field"):
        asyncio.run(collect())
    assert _FakeClient.instances[0].disconnected is True


def test_adapter_disconnects_when_consumer_cancels(monkeypatch) -> None:
    class BlockingRawQuery:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def receive_messages(self):
            self.started.set()
            await asyncio.Event().wait()
            yield {}  # pragma: no cover

    class BlockingClient(_FakeClient):
        def __init__(self, options: ClaudeAgentOptions) -> None:
            super().__init__(options)
            self._query = BlockingRawQuery()

    BlockingClient.instances = []
    monkeypatch.setattr(prompt_suggestions, "ClaudeSDKClient", BlockingClient)
    monkeypatch.setattr(prompt_suggestions, "_prompt_suggestions_supported", _supported)

    async def scenario() -> None:
        async def collect() -> None:
            async for _ in query_with_prompt_suggestions(prompt="hello", options=ClaudeAgentOptions()):
                pass

        task = asyncio.create_task(collect())
        while not BlockingClient.instances:
            await asyncio.sleep(0)
        await BlockingClient.instances[0]._query.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert BlockingClient.instances[0].disconnected is True


def test_runtime_emits_backend_owned_suggestion_without_persisting_it(tmp_path, monkeypatch) -> None:
    async def fake_query(*, prompt, options):
        await anext(prompt)
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "prompt-suggestion-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="answer")], model="test-model", session_id=sdk_session_id)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
            result="answer",
        )
        yield PromptSuggestionMessage("下一步检查边界条件", uuid="hostile-run", session_id="hostile-session")

    monkeypatch.setattr(prompt_suggestions, "query_with_prompt_suggestions", fake_query)
    monkeypatch.setattr("app.runtime.claude_runtime_stream.read_requires_web_hitl", lambda _workspace: False)
    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.main_workspace_dir)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), feedback_store=store)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="hello", session_id="api-session"))]

    events = asyncio.run(collect())
    suggestion = next(event for event in events if event["event"] == "prompt_suggestion")
    result = next(event for event in events if event["event"] == "result")
    record = store.find_run(run_id=result["data"]["run_id"])

    assert suggestion["data"] == {
        "suggestion": "下一步检查边界条件",
        "run_id": result["data"]["run_id"],
        "session_id": "api-session",
    }
    assert events.index(suggestion) > events.index(result)
    assert events[-1]["event"] == "done"
    assert record is not None
    assert all(message.get("event") != "PromptSuggestionMessage" for message in record["messages"])
    assert "下一步检查边界条件" not in str(record)
