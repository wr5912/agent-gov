"""AgentScope 原生 Msg 与同 run durable 回执的无正文工具活动投影。"""

from __future__ import annotations

import json

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from app.runtime_gateway._execution_support import _tool_activity_from_current_run
from app.runtime_gateway.contracts import RuntimeTraceExpectations, RuntimeTraceToolExpectation
from app.runtime_gateway.store import RuntimeStateConflict


def _reply(reply_id: str, *blocks: TextBlock | ToolCallBlock | ToolResultBlock) -> dict:
    return Msg(id=reply_id, name="assistant", role="assistant", content=list(blocks)).model_dump(mode="json")


def _expectations(*, reply_ids: list[str], tool_results: list[RuntimeTraceToolExpectation] | None = None) -> RuntimeTraceExpectations:
    return RuntimeTraceExpectations(
        run_id="run-current",
        root_session_id="session-current",
        root_reply_ids=reply_ids,
        tool_results=tool_results or [],
    )


def _activity(messages: list[dict], expectations: RuntimeTraceExpectations) -> list[dict] | None:
    return _tool_activity_from_current_run(
        messages=messages,
        reply_ids=expectations.root_reply_ids,
        run_id="run-current",
        session_id="session-current",
        expectations=expectations,
    )


def test_current_run_canonical_tool_call_and_result_keep_only_metadata() -> None:
    current = _reply(
        "reply-current",
        ToolCallBlock(id="call-read", name="Read", input='{"path":"private-source"}', state="finished"),
        ToolResultBlock(id="call-read", name="Read", output="private-result", state="success"),
        TextBlock(text="done"),
    )
    stale = _reply(
        "reply-previous",
        ToolCallBlock(id="call-stale", name="Write", input='{"secret":"old"}', state="finished"),
    )
    expectations = _expectations(
        reply_ids=["reply-current"],
        tool_results=[
            RuntimeTraceToolExpectation(
                session_id="session-current",
                reply_id="reply-current",
                tool_call_id="call-read",
                state="success",
                source="tool_result_receipt",
            )
        ],
    )

    activity = _activity([stale, current], expectations)

    assert activity == [
        {
            "id": "call-read",
            "name": "Read",
            "state": "success",
            "reply_id": "reply-current",
            "session_id": "session-current",
        }
    ]
    encoded = json.dumps(activity)
    assert "private-source" not in encoded
    assert "private-result" not in encoded
    assert "call-stale" not in encoded


def test_complete_current_run_without_tools_is_proven_empty() -> None:
    reply = _reply("reply-current", TextBlock(text="hello"))

    assert _activity([reply], _expectations(reply_ids=["reply-current"])) == []


def test_missing_canonical_reply_or_incomplete_control_is_unknown_not_empty() -> None:
    reply = _reply("reply-current", TextBlock(text="hello"))
    missing = _expectations(reply_ids=["reply-current", "reply-unavailable"])
    incomplete = _expectations(reply_ids=["reply-current"]).model_copy(update={"control_integrity_complete": False})

    assert _activity([reply], missing) is None
    assert _activity([reply], incomplete) is None


def test_child_tool_receipt_is_nonempty_even_without_child_message_name() -> None:
    reply = _reply("reply-current", TextBlock(text="done"))
    expectations = _expectations(
        reply_ids=["reply-current"],
        tool_results=[
            RuntimeTraceToolExpectation(
                session_id="session-child",
                reply_id="reply-child",
                tool_call_id="call-child",
                state="denied",
                source="tool_result_receipt",
            )
        ],
    )

    assert _activity([reply], expectations) == [
        {
            "id": "call-child",
            "name": None,
            "state": "denied",
            "reply_id": "reply-child",
            "session_id": "session-child",
        }
    ]


def test_tool_receipt_identity_mismatch_fails_closed() -> None:
    reply = _reply("reply-current", TextBlock(text="done"))
    mismatched = _expectations(reply_ids=["reply-current"]).model_copy(update={"run_id": "run-other"})

    with pytest.raises(RuntimeStateConflict, match="does not match the current run"):
        _activity([reply], mismatched)
