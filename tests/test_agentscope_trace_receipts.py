from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
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
from agentscope_runtime.context_registry import RuntimeContext, take_reply_context
from agentscope_runtime.observability import RedactingSpanProcessor
from agentscope_runtime.receipt_middleware import CURRENT_RUNTIME_CONTEXT, AgentGovReceiptMiddleware
from agentscope_runtime.settings import RuntimeSettings
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def _settings(tmp_path: Path) -> RuntimeSettings:
    roots = [tmp_path / name for name in ("business", "candidates", "workspaces")]
    for root in roots:
        root.mkdir()
    data_dir = tmp_path / "data"
    return RuntimeSettings(
        shared_secret="shared-test-secret",
        provider_api_key="provider-test-secret",
        agentgov_api_base_url="http://agent-gov-api:8080",
        provider_api_url="http://model-provider.test/v1",
        data_dir=data_dir,
        business_agents_root=roots[0],
        candidates_root=roots[1],
        workspaces_root=roots[2],
        database_url=f"sqlite+aiosqlite:///{data_dir / 'agentscope.db'}",
    )


@pytest.fixture(autouse=True)
def _clear_reply_context() -> Iterator[None]:
    take_reply_context("session-1", "reply-1")
    yield
    take_reply_context("session-1", "reply-1")


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
    provider.add_span_processor(RedactingSpanProcessor(SimpleSpanProcessor(exporter)))
    return provider.get_tracer("test-agentgov-receipts"), exporter


def _tool_call() -> ToolCallBlock:
    return ToolCallBlock(id="call-1", name="Read", input='{"file_path":"README.md"}')


def test_tool_result_end_posts_body_free_control_receipt_and_annotates_tool_span(tmp_path: Path) -> None:
    posted: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    middleware = AgentGovReceiptMiddleware(_settings(tmp_path), transport=httpx.MockTransport(handler))
    event = ToolResultEndEvent(
        id="tool-end-1",
        reply_id="reply-1",
        tool_call_id="call-1",
        state=ToolResultState.SUCCESS,
    )
    agent = SimpleNamespace(state=SimpleNamespace(session_id="session-1", reply_id="reply-1"))
    tracer, exporter = _tracer()

    async def tool_handler(**_: object):
        yield event

    async def with_acting(**kwargs: object):
        async for item in middleware.on_acting(agent, kwargs, tool_handler):
            yield item

    async def exercise() -> list[object]:
        with tracer.start_as_current_span(  # type: ignore[union-attr]
            "execute_tool Read",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.call.result": "provider-test-secret",
            },
        ) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            token = CURRENT_RUNTIME_CONTEXT.set(_context(trace_id))
            try:
                return [item async for item in middleware.on_reply(agent, {"inputs": None}, with_acting)]
            finally:
                CURRENT_RUNTIME_CONTEXT.reset(token)

    assert asyncio.run(exercise()) == [event]
    assert posted == [
        {
            "event_id": "tool-end-1",
            "payload": {"state": "success", "tool_call_id": "call-1"},
            "receipt_id": hashlib.sha256(b"run-1\nsession-1\ntool-end-1").hexdigest(),
            "reply_id": "reply-1",
            "run_id": "run-1",
            "session_id": "session-1",
            "trace_id": posted[0]["trace_id"],
            "type": "TOOL_RESULT_END",
        },
    ]
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
            RequireUserConfirmEvent(reply_id="reply-1", tool_calls=[_tool_call()]),
            "agentscope.agent.hitl_pending_tool_call_ids",
        ),
        (
            RequireExternalExecutionEvent(reply_id="reply-1", tool_calls=[_tool_call()]),
            "agentscope.agent.external_execution_pending_tool_call_ids",
        ),
    ],
)
def test_pending_action_receipt_annotates_request_with_durable_tool_identity(
    tmp_path: Path,
    event: object,
    attribute: str,
) -> None:
    middleware = AgentGovReceiptMiddleware(
        _settings(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"run_id": "run-1", "status": "running"}),
        ),
    )
    agent = SimpleNamespace(state=SimpleNamespace(session_id="session-1", reply_id="reply-1"))
    tracer, exporter = _tracer()

    async def next_handler(**_: object):
        yield event

    async def exercise() -> None:
        with tracer.start_as_current_span(  # type: ignore[union-attr]
            "invoke_agent",
            attributes={"gen_ai.operation.name": "invoke_agent"},
        ) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            token = CURRENT_RUNTIME_CONTEXT.set(_context(trace_id))
            try:
                _ = [item async for item in middleware.on_reply(agent, {"inputs": None}, next_handler)]
            finally:
                CURRENT_RUNTIME_CONTEXT.reset(token)

    asyncio.run(exercise())
    assert exporter.get_finished_spans()[0].attributes[attribute] == ("call-1",)


@pytest.mark.parametrize(
    ("event", "event_type"),
    [
        (
            UserConfirmResultEvent(
                reply_id="reply-1",
                confirm_results=[ConfirmResult(confirmed=True, tool_call=_tool_call())],
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
def test_continuation_span_records_only_native_event_type_and_reply_id(
    tmp_path: Path,
    event: object,
    event_type: str,
) -> None:
    middleware = AgentGovReceiptMiddleware(_settings(tmp_path))
    agent = SimpleNamespace(state=SimpleNamespace(session_id="session-1", reply_id="reply-1"))
    tracer, exporter = _tracer()

    async def no_events(**_: object):
        if False:  # pragma: no cover - 保持 AsyncGenerator 契约
            yield None

    async def exercise() -> None:
        with tracer.start_as_current_span(  # type: ignore[union-attr]
            "invoke_agent",
            attributes={"gen_ai.operation.name": "invoke_agent"},
        ) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            token = CURRENT_RUNTIME_CONTEXT.set(_context(trace_id))
            try:
                _ = [item async for item in middleware.on_reply(agent, {"inputs": event}, no_events)]
            finally:
                CURRENT_RUNTIME_CONTEXT.reset(token)

    asyncio.run(exercise())
    exported = exporter.get_finished_spans()[0]
    assert exported.attributes["agentscope.agent.incoming_event_type"] == event_type
    assert exported.attributes["agentscope.agent.reply_id"] == "reply-1"
    assert "provider-test-secret" not in exported.to_json()
