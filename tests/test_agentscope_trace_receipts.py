from __future__ import annotations

import hashlib
import json

import pytest
from agentscope.event import (
    ConfirmResult,
    ExternalExecutionResultEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
)
from agentscope.message import ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope_runtime.context_registry import RuntimeContext
from agentscope_runtime.observability import RedactingSpanProcessor
from agentscope_runtime.receipt_middleware import (
    _annotate_incoming_event,
    annotate_tool_result,
    build_interrupted_receipt,
    build_runtime_receipt,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _context(trace_id: str) -> RuntimeContext:
    return RuntimeContext(
        run_id="run-1",
        session_id="session-1",
        root_session_id="session-1",
        role="root",
        agent_id="agent-1",
        agent_version_id="version-1",
        runtime_agent_id="runtime-agent-1",
        harness_digest="e" * 64,
        trace_id=trace_id,
        team_generation=0,
    )


def _tracer() -> tuple[object, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        RedactingSpanProcessor(SimpleSpanProcessor(exporter)),
    )
    return provider.get_tracer("test-agentgov-receipts"), exporter


def _tool_call() -> ToolCallBlock:
    return ToolCallBlock(
        id="call-1",
        name="Read",
        input='{"file_path":"hitl-private-canary.txt"}',
    )


def test_tool_result_event_builds_body_free_receipt_and_safe_span() -> None:
    event = ToolResultEndEvent(
        id="tool-end-1",
        reply_id="reply-1",
        tool_call_id="call-1",
        state=ToolResultState.SUCCESS,
    )
    tracer, exporter = _tracer()

    with tracer.start_as_current_span(
        "execute_tool Read",
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.call.result": "provider-test-secret",
        },
    ) as span:
        trace_id = f"{span.get_span_context().trace_id:032x}"
        receipt = build_runtime_receipt(
            _context(trace_id),
            event,
            fallback_reply_id="reply-1",
        )
        annotate_tool_result("session-1", event)

    assert receipt.model_dump(mode="json") == {
        "event_id": "tool-end-1",
        "payload": {"state": "success", "tool_call_id": "call-1"},
        "receipt_id": hashlib.sha256(
            b"run-1\nsession-1\ntool-end-1",
        ).hexdigest(),
        "reply_id": "reply-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "trace_id": trace_id,
        "type": "TOOL_RESULT_END",
    }
    exported = exporter.get_finished_spans()[0]
    assert exported.name == "execute_tool"
    assert exported.attributes["gen_ai.tool.call.id"] == "call-1"
    assert exported.attributes["agentscope.agent.reply_id"] == "reply-1"
    assert exported.attributes["agentscope.tool.result.state"] == "success"
    assert (
        exported.attributes["agentgov.content.tool_result.sha256"]
        == hashlib.sha256(
            b"provider-test-secret",
        ).hexdigest()
    )
    assert "provider-test-secret" not in exported.to_json()


@pytest.mark.parametrize(
    ("event", "attribute"),
    [
        (
            RequireUserConfirmEvent(
                reply_id="reply-1",
                tool_calls=[_tool_call()],
            ),
            "agentscope.agent.hitl_pending_tool_call_ids",
        ),
        (
            RequireExternalExecutionEvent(
                reply_id="reply-1",
                tool_calls=[_tool_call()],
            ),
            "agentscope.agent.external_execution_pending_tool_call_ids",
        ),
    ],
)
def test_pending_action_receipt_annotates_durable_tool_identity(
    event: object,
    attribute: str,
) -> None:
    tracer, exporter = _tracer()
    with tracer.start_as_current_span(
        "invoke_agent",
        attributes={"gen_ai.operation.name": "invoke_agent"},
    ) as span:
        trace_id = f"{span.get_span_context().trace_id:032x}"
        receipt = build_runtime_receipt(
            _context(trace_id),
            event,
            fallback_reply_id="reply-1",
        )

    native_tool_call = event.tool_calls[0].model_dump(mode="json")
    canonical = json.dumps(
        native_tool_call,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    assert receipt.payload == {
        "tool_calls": [
            {
                "tool_call_id": "call-1",
                "tool_call_name": "Read",
                "tool_call_state": native_tool_call["state"],
                "tool_call_utf8_length": len(canonical),
                "tool_call_sha256": hashlib.sha256(canonical).hexdigest(),
            },
        ],
    }
    assert "hitl-private-canary" not in receipt.model_dump_json()
    assert exporter.get_finished_spans()[0].attributes[attribute] == ("call-1",)


@pytest.mark.parametrize(
    ("event", "event_type"),
    [
        (
            UserConfirmResultEvent(
                reply_id="reply-1",
                confirm_results=[
                    ConfirmResult(confirmed=True, tool_call=_tool_call()),
                ],
            ),
            "USER_CONFIRM_RESULT",
        ),
        (
            ExternalExecutionResultEvent(
                reply_id="reply-1",
                execution_results=[
                    ToolResultBlock(
                        id="call-1",
                        name="remote",
                        output="provider-test-secret",
                        state=ToolResultState.SUCCESS,
                    ),
                ],
            ),
            "EXTERNAL_EXECUTION_RESULT",
        ),
    ],
)
def test_continuation_span_records_only_native_identity(
    event: object,
    event_type: str,
) -> None:
    tracer, exporter = _tracer()
    with tracer.start_as_current_span(
        "invoke_agent",
        attributes={"gen_ai.operation.name": "invoke_agent"},
    ):
        _annotate_incoming_event(event)

    exported = exporter.get_finished_spans()[0]
    assert exported.attributes["agentscope.agent.incoming_event_type"] == event_type
    assert exported.attributes["agentscope.agent.reply_id"] == "reply-1"
    assert "provider-test-secret" not in exported.to_json()


def test_interrupted_receipt_is_deterministic_and_contains_no_business_body() -> None:
    context = _context("a" * 32)

    first = build_interrupted_receipt(context)
    repeated = build_interrupted_receipt(context)

    assert repeated == first
    assert first.type == "RUN_INTERRUPTED"
    assert first.run_id == context.run_id
    assert first.session_id == context.session_id
    assert first.trace_id == context.trace_id
    assert first.reply_id is None
    assert first.payload == {}
