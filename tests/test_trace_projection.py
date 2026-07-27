from __future__ import annotations

from app.runtime.agent_trace import AgentTraceProjector, project_agent_trace
from app.runtime.native_chat_stream import NativeChatSemanticProjector


def test_projector_suppresses_thinking_token_counters_but_keeps_complete_sdk_facts() -> None:
    messages = [
        {
            "event": "SystemMessage:thinking_tokens",
            "subtype": "thinking_tokens",
            "data": {"tokens": 8},
        },
        {
            "event": "AssistantMessage",
            "content": [
                {"thinking": "先检查 workspace，再汇总能力。", "signature": "opaque"},
                {"name": "Glob", "id": "tool-1", "input": {"pattern": "**/SKILL.md"}},
                {"name": "Read", "id": "tool-2", "input": {"file_path": "AGENTS.md"}},
                {"text": "检查完成。"},
            ],
            "parent_tool_use_id": "task-parent",
            "session_id": "sdk-1",
        },
        {
            "event": "UserMessage",
            "content": [
                {"tool_use_id": "tool-1", "content": ["skill-a", "skill-b"], "is_error": False},
                {"tool_use_id": "tool-2", "content": "rules", "is_error": False},
            ],
        },
        {
            "event": "HookEventMessage:PostToolUse",
            "hook_event_name": "PostToolUse",
            "data": {"tool_name": "Read"},
        },
        {
            "event": "TaskStartedMessage",
            "task_id": "task-1",
            "description": "scan",
        },
        {
            "event": "ResultMessage:success",
            "subtype": "success",
            "is_error": False,
            "result": "done",
        },
    ]

    events = project_agent_trace("run-1", messages)

    assert [event.kind for event in events] == [
        "thinking",
        "tool_use",
        "tool_use",
        "text",
        "tool_result",
        "tool_result",
        "hook",
        "task",
        "result",
    ]
    assert [event.sequence for event in events] == list(range(1, 10))
    assert {event.message_index for event in events[:4]} == {1}
    assert events[0].payload["thinking"] == "先检查 workspace，再汇总能力。"
    assert "signature" not in events[0].payload
    assert [event.payload["tool_name"] for event in events if event.kind == "tool_use"] == ["Glob", "Read"]
    assert events[0].scope == "subagent"
    assert events[0].parent_tool_use_id == "task-parent"
    assert events[7].subagent_id == "task-1"


def test_projector_event_ids_are_stable_and_backend_owned() -> None:
    message = {
        "event": "AssistantMessage",
        "content": [
            {
                "text": "answer",
                "event_id": "client-owned",
                "run_id": "client-run",
                "sequence": 999,
            }
        ],
    }

    first = project_agent_trace("run-safe", [message])[0]
    second = project_agent_trace("run-safe", [message])[0]

    assert first == second
    assert first.run_id == "run-safe"
    assert first.sequence == 1
    assert first.event_id.startswith("trace_")
    assert first.payload["text"] == "answer"


def test_incremental_projector_counts_suppressed_messages_for_refresh_parity() -> None:
    projector = AgentTraceProjector("run-2")

    assert projector.project_message({"event": "SystemMessage:thinking_tokens", "subtype": "thinking_tokens"}) == []
    event = projector.project_message({"event": "AssistantMessage", "content": [{"thinking": "full"}]})[0]

    assert event.message_index == 1
    assert event.sequence == 1


def test_native_semantic_projection_keeps_text_and_replaces_raw_noise_with_trace() -> None:
    projector = NativeChatSemanticProjector()
    session = {"event": "session", "data": {"run_id": "run-3", "session_id": "session-3"}}
    delta = {
        "event": "message",
        "data": {"event": "StreamEvent", "text": "答", "text_kind": "delta", "raw": {}},
    }
    counter = {
        "event": "message",
        "data": {
            "event": "SystemMessage:thinking_tokens",
            "text": "",
            "text_kind": "snapshot",
            "raw": {"event": "SystemMessage:thinking_tokens", "subtype": "thinking_tokens"},
        },
    }
    snapshot = {
        "event": "message",
        "data": {
            "event": "AssistantMessage",
            "text": "答案",
            "text_kind": "snapshot",
            "raw": {
                "event": "AssistantMessage",
                "content": [
                    {"thinking": "完整思考"},
                    {"name": "Read", "id": "tool-1", "input": {"file_path": "AGENTS.md"}},
                    {"text": "答案"},
                ],
            },
        },
    }

    assert projector.project(session) == [session]
    assert projector.project(delta) == [delta]
    assert projector.project(counter) == []
    projected = projector.project(snapshot)

    assert projected[0] == snapshot
    assert [frame["event"] for frame in projected[1:]] == ["trace_event", "trace_event", "trace_event"]
    assert [frame["data"]["kind"] for frame in projected[1:]] == ["thinking", "tool_use", "text"]
