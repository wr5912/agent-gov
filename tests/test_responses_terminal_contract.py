from __future__ import annotations

import asyncio
import json

import pytest
from app.runtime.managed_claude_events import AgentGovControlEvent
from app.runtime.openai_responses_stream import iter_responses_sse


def _speech() -> AgentGovControlEvent:
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


def _names(text: str) -> list[str]:
    return [line.removeprefix("event: ") for line in text.splitlines() if line.startswith("event: ")]


async def _collect(source, *, control: bool = True) -> str:
    return "".join(
        [
            chunk
            async for chunk in iter_responses_sse(
                source,
                model="test",
                effective_agent_id="agent-1",
                control=control,
            )
        ]
    )


def test_success_terminal_is_exactly_once_and_last_after_derived_events() -> None:
    async def source():
        yield AgentGovControlEvent(
            name="session",
            data={"run_id": "run-1", "session_id": "session-1"},
        )
        yield AgentGovControlEvent(
            name="result",
            data={
                "run_id": "run-1",
                "session_id": "session-1",
                "errors": [],
            },
        )
        yield AgentGovControlEvent(
            name="prompt_suggestion",
            data={"suggestion": "继续核对", "suggestions": ["继续核对"]},
        )
        yield _speech()
        yield AgentGovControlEvent(name="done", data={})
        yield _speech()
        yield AgentGovControlEvent(
            name="prompt_suggestion",
            data={"suggestion": "迟到事件"},
        )

    names = _names(asyncio.run(_collect(source())))

    assert names.count("response.completed") == 1
    assert "response.failed" not in names
    assert names[-2:] == ["agentgov.done", "response.completed"]
    assert names.index("agentgov.prompt_suggestion") < names.index("agentgov.done")
    assert names.index("agentgov.speech_summary") < names.index("agentgov.done")
    assert names.count("agentgov.speech_summary") == 1


@pytest.mark.parametrize("control", [False, True])
def test_source_exception_has_one_failed_terminal_and_it_is_last(
    control: bool,
) -> None:
    async def source():
        if False:
            yield AgentGovControlEvent(name="done", data={})
        raise RuntimeError("source failed")

    names = _names(asyncio.run(_collect(source(), control=control)))

    assert names.count("response.failed") == 1
    assert "response.completed" not in names
    assert names[-1] == "response.failed"
    if control:
        assert names[-2] == "agentgov.done"
        assert names.count("agentgov.error") == 1
    else:
        assert all(not name.startswith("agentgov.") for name in names)


def test_duplicate_failure_facts_do_not_duplicate_or_erase_terminal_response() -> None:
    error_data = {
        "run_id": "run-1",
        "session_id": "session-1",
        "errors": ["runtime failed"],
    }

    async def source():
        yield AgentGovControlEvent(
            name="session",
            data={"run_id": "run-1", "session_id": "session-1"},
        )
        yield AgentGovControlEvent(name="result", data=error_data)
        yield AgentGovControlEvent(name="error", data=error_data)
        yield AgentGovControlEvent(name="done", data={})

    text = asyncio.run(_collect(source()))
    names = _names(text)
    failed_block = next(block for block in text.split("\n\n") if "event: response.failed\n" in block)
    failed_data = json.loads(next(line.removeprefix("data: ") for line in failed_block.splitlines() if line.startswith("data: ")))

    assert names.count("agentgov.error") == 1
    assert names.count("response.failed") == 1
    assert names[-2:] == ["agentgov.done", "response.failed"]
    assert failed_data["response"]["status"] == "failed"
