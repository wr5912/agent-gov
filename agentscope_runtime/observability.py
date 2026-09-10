"""AgentScope TracingMiddleware 的 OpenTelemetry 出口与生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status
from opentelemetry.util.types import AttributeValue
from starlette.types import ASGIApp, Receive, Scope, Send

from .otel_config import runtime_otel_config
from .run_trace import AgentGovTraceIdGenerator

_SAFE_SPAN_ATTRIBUTES = frozenset(
    {
        "agentgov.run.id",
        "agentgov.run.stage",
        "agentgov.run.finished_reason",
        "agentgov.agent.id",
        "agentgov.agent.version_id",
        "agentgov.harness.digest",
        "agentgov.runtime.version",
        "agentgov.content.input.length",
        "agentgov.content.input.sha256",
        "agentgov.content.output.length",
        "agentgov.content.output.sha256",
        "agentgov.content.tool_definitions.length",
        "agentgov.content.tool_definitions.sha256",
        "agentgov.content.tool_arguments.length",
        "agentgov.content.tool_arguments.sha256",
        "agentgov.content.tool_result.length",
        "agentgov.content.tool_result.sha256",
        "agentgov.event.id",
        "agentgov.event.type",
        "agentgov.receipt.id",
        "agentscope.agent.id",
        "agentscope.runtime.version",
        "agentscope.session.id",
        "agentscope.agent.reply_id",
        "agentscope.agent.hitl_pending_tools",
        "agentscope.agent.hitl_pending_tool_call_ids",
        "agentscope.agent.external_execution_pending_tools",
        "agentscope.agent.external_execution_pending_tool_call_ids",
        "agentscope.agent.incoming_event_type",
        "agentscope.agent.is_external_execution",
        "agentscope.tool.result.state",
        "agentscope.usage.cache_input_tokens",
        "agentscope.usage.cache_creation_input_tokens",
        "gen_ai.conversation.id",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.id",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.agent.id",
        "gen_ai.tool.call.id",
        "gen_ai.tool.name",
    },
)
_SAFE_RESOURCE_ATTRIBUTES = frozenset(
    {
        "service.name",
        "service.version",
        "service.instance.id",
        "deployment.environment.name",
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
    },
)
_SAFE_SPAN_NAMES = frozenset({"agentgov.run", "agentgov.run.stage", "chat", "execute_tool", "invoke_agent"})
_CONTENT_SPAN_ATTRIBUTES = {
    "gen_ai.input.messages": "input",
    "gen_ai.output.messages": "output",
    "gen_ai.tool.definitions": "tool_definitions",
    "gen_ai.tool.call.arguments": "tool_arguments",
    "gen_ai.tool.call.result": "tool_result",
}


def _filtered_attributes(
    attributes: Mapping[str, AttributeValue] | None,
    allowed: frozenset[str],
) -> Mapping[str, AttributeValue]:
    return {key: value for key, value in (attributes or {}).items() if key in allowed}


def _safe_span_name(span: ReadableSpan) -> str:
    if span.name in {"agentgov.run", "agentgov.run.stage"}:
        return span.name
    operation = (span.attributes or {}).get("gen_ai.operation.name")
    return str(operation) if operation in _SAFE_SPAN_NAMES else "agentscope.operation"


def _content_fingerprints(attributes: Mapping[str, AttributeValue] | None) -> dict[str, AttributeValue]:
    """保留可对账的字节长度/hash，不把消息或工具正文送出 Runtime。"""

    fingerprints: dict[str, AttributeValue] = {}
    for source, label in _CONTENT_SPAN_ATTRIBUTES.items():
        value = (attributes or {}).get(source)
        if value is None:
            continue
        if isinstance(value, str):
            canonical = value
        else:
            canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        encoded = canonical.encode("utf-8")
        fingerprints[f"agentgov.content.{label}.length"] = len(encoded)
        fingerprints[f"agentgov.content.{label}.sha256"] = hashlib.sha256(encoded).hexdigest()
    return fingerprints


def _redacted_span(span: ReadableSpan) -> ReadableSpan:
    """Copy only non-content metadata into the exporter-facing span."""

    resource = Resource(
        _filtered_attributes(span.resource.attributes, _SAFE_RESOURCE_ATTRIBUTES),
        schema_url=span.resource.schema_url,
    )
    safe_attributes = {
        **_filtered_attributes(span.attributes, _SAFE_SPAN_ATTRIBUTES),
        **_content_fingerprints(span.attributes),
    }
    return ReadableSpan(
        name=_safe_span_name(span),
        context=span.context,
        parent=span.parent,
        resource=resource,
        attributes=safe_attributes,
        events=(),
        links=(),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanProcessor(SpanProcessor):
    """Drop prompts, messages, tool I/O, errors, and unknown metadata before export."""

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        self._delegate.on_end(_redacted_span(span))

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._delegate.force_flush(timeout_millis)


@dataclass
class OTelRuntime:
    """Own one Runtime-created provider and shut it down exactly once."""

    provider: TracerProvider
    flush_timeout_millis: int = 10_000
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.provider.force_flush(timeout_millis=self.flush_timeout_millis)
        self.provider.shutdown()


class OTelRuntimeLifecycleMiddleware:
    """Flush and close the Runtime-owned provider after ASGI lifespan exit."""

    def __init__(self, app: ASGIApp, *, runtime: OTelRuntime) -> None:
        self._app = app
        self._runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._app(scope, receive, send)
        finally:
            if scope["type"] == "lifespan":
                await asyncio.to_thread(self._runtime.shutdown)


_managed_runtime: OTelRuntime | None = None


def configure_otel_from_env() -> OTelRuntime | None:
    """仅在统一开关启用且摄取配置完整时初始化脱敏 OTLP 出口。"""

    global _managed_runtime  # noqa: PLW0603
    config = runtime_otel_config(os.environ)
    if config is None:
        return None
    current = trace.get_tracer_provider()
    if _managed_runtime is not None and current is _managed_runtime.provider:
        return _managed_runtime
    if isinstance(current, TracerProvider):
        raise RuntimeError("OpenTelemetry provider was configured outside AgentGov Runtime")

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME") or "agent-gov-agentscope-runtime",
            "service.version": os.environ.get("AGENTGOV_RUNTIME_VERSION", "dev"),
        },
    )
    if "deployment.environment.name" not in resource.attributes:
        resource = resource.merge(Resource({"deployment.environment.name": "local"}))
    provider = TracerProvider(id_generator=AgentGovTraceIdGenerator(), resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=config.endpoint, headers=config.headers))
    provider.add_span_processor(RedactingSpanProcessor(processor))
    trace.set_tracer_provider(provider)
    _managed_runtime = OTelRuntime(provider)
    return _managed_runtime
