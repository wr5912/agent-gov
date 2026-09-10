"""将 AgentScope 原生 AgentEvent 以签名回执投影给 AgentGov。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

import httpx
from agentscope.event import (
    EventBase,
    ExternalExecutionResultEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
)
from agentscope.middleware import MiddlewareBase
from opentelemetry import trace
from opentelemetry.util.types import AttributeValue
from pydantic import BaseModel, ConfigDict

from .context_registry import RuntimeContext, bind_reply_context
from .settings import RuntimeSettings
from .signing import signed_headers
from .types import JsonObject, MiddlewareInput


class RuntimeReceipt(BaseModel):
    """AgentScope 原生事件的 AgentGov HTTP 边界回执。"""

    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    event_id: str
    run_id: str
    session_id: str
    reply_id: str | None
    trace_id: str | None
    type: str
    payload: JsonObject


class RuntimeReceiptAck(BaseModel):
    """AgentGov 接收回执后的权威 run 状态；Storage 用它结束 trace root。"""

    run_id: str
    status: str
    terminal_reason: str | None = None


CURRENT_RUNTIME_CONTEXT: ContextVar[RuntimeContext | None] = ContextVar(
    "agentgov_runtime_context",
    default=None,
)

# AgentGov's control plane only consumes reply lifecycle and HITL transitions.
# High-volume model/tool/block events remain available through OpenTelemetry;
# synchronously posting them would add one HTTP + SQLite round trip per token.
_CONTROL_RECEIPT_TYPES = {
    "REPLY_START",
    "REQUIRE_USER_CONFIRM",
    "REQUIRE_EXTERNAL_EXECUTION",
    "TOOL_RESULT_END",
    "REPLY_END",
}


def _current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}"


def _canonical_json(payload: BaseModel) -> bytes:
    return json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


async def fetch_runtime_context(
    client: httpx.AsyncClient,
    settings: RuntimeSettings,
    session_id: str,
) -> RuntimeContext:
    """读取一个 session 的权威 active-run 上下文。"""

    path = f"/internal/runtime-context/{quote(session_id, safe='')}"
    response = await client.get(
        path,
        headers=signed_headers(settings.shared_secret, "GET", path),
    )
    response.raise_for_status()
    context = RuntimeContext.model_validate(response.json())
    if context.session_id != session_id:
        raise ValueError("runtime context session_id does not match AgentScope session")
    return context


async def post_runtime_receipt(
    client: httpx.AsyncClient,
    settings: RuntimeSettings,
    receipt: RuntimeReceipt,
) -> RuntimeReceiptAck:
    """按固定 raw-body 签名提交一条回执。"""

    path = "/internal/runtime-receipts"
    body = _canonical_json(receipt)
    headers = signed_headers(settings.shared_secret, "POST", path, body)
    headers["Content-Type"] = "application/json"
    response = await client.post(path, content=body, headers=headers)
    response.raise_for_status()
    return RuntimeReceiptAck.model_validate(response.json())


class AgentGovReceiptMiddleware(MiddlewareBase):
    """Fail closed when a run lacks context or an event cannot be receipted."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: MiddlewareInput,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """Resolve one run context, then receipt every full native event."""

        _annotate_incoming_event(input_kwargs.get("inputs"))
        async with httpx.AsyncClient(
            base_url=self._settings.agentgov_api_base_url,
            timeout=self._settings.request_timeout_seconds,
            transport=self._transport,
        ) as client:
            context = CURRENT_RUNTIME_CONTEXT.get()
            if context is None:
                context = await fetch_runtime_context(
                    client,
                    self._settings,
                    agent.state.session_id,
                )
            async for item in next_handler(**input_kwargs):
                if isinstance(item, EventBase) and item.type in _CONTROL_RECEIPT_TYPES:
                    receipt = self._build_receipt(context, agent, item)
                    if receipt.reply_id is not None:
                        bind_reply_context(context, receipt.reply_id)
                    await self._post_receipt(client, receipt)
                yield item

    async def _post_receipt(
        self,
        client: httpx.AsyncClient,
        receipt: RuntimeReceipt,
    ) -> None:
        await post_runtime_receipt(client, self._settings, receipt)

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: MiddlewareInput,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """把无正文的工具终态身份写入当前 execute_tool span。"""

        async for item in next_handler(**input_kwargs):
            if isinstance(item, ToolResultEndEvent):
                state = getattr(item.state, "value", item.state)
                trace.get_current_span().set_attributes(
                    {
                        "agentscope.session.id": str(agent.state.session_id),
                        "agentscope.agent.reply_id": item.reply_id,
                        "agentscope.tool.result.state": str(state),
                        "gen_ai.tool.call.id": item.tool_call_id,
                    },
                )
            yield item

    @staticmethod
    def _build_receipt(
        context: RuntimeContext,
        agent: Any,
        event: EventBase,
    ) -> RuntimeReceipt:
        native_payload = event.model_dump(mode="json")
        event_id = str(native_payload["id"])
        reply_id = str(native_payload.get("reply_id") or agent.state.reply_id)
        receipt_id = hashlib.sha256(
            f"{context.run_id}\n{context.session_id}\n{event_id}".encode(),
        ).hexdigest()
        current_trace_id = _current_trace_id()
        if current_trace_id is not None and current_trace_id != context.trace_id:
            raise ValueError("active OpenTelemetry trace does not match Runtime context")
        receipt = RuntimeReceipt(
            receipt_id=receipt_id,
            event_id=event_id,
            run_id=context.run_id,
            session_id=context.session_id,
            reply_id=reply_id,
            trace_id=context.trace_id,
            type=str(native_payload["type"]),
            payload=_receipt_payload(native_payload),
        )
        span = trace.get_current_span()
        span_attributes: dict[str, AttributeValue] = {
            "agentgov.run.id": context.run_id,
            "agentscope.session.id": context.session_id,
            "agentgov.agent.id": context.agent_id,
            "agentgov.agent.version_id": context.agent_version_id,
            "agentscope.agent.id": context.runtime_agent_id,
            "agentgov.harness.digest": context.harness_digest,
            "agentscope.agent.reply_id": reply_id,
            "agentgov.event.id": event_id,
            "agentgov.event.type": receipt.type,
            "agentgov.receipt.id": receipt_id,
        }
        span_attributes.update(_receipt_span_attributes(receipt))
        span.set_attributes(span_attributes)
        return receipt


def _receipt_payload(native_payload: JsonObject) -> JsonObject:
    """只投影控制面状态机所需字段，禁止回执携带消息/工具结果正文。"""

    event_type = native_payload.get("type")
    if event_type in {"REQUIRE_USER_CONFIRM", "REQUIRE_EXTERNAL_EXECUTION"}:
        tool_calls = native_payload.get("tool_calls")
        return {"tool_calls": tool_calls} if isinstance(tool_calls, list) else {}
    if event_type == "TOOL_RESULT_END":
        tool_call_id = native_payload.get("tool_call_id")
        state = native_payload.get("state")
        if isinstance(tool_call_id, str) and isinstance(state, str):
            return {"tool_call_id": tool_call_id, "state": state}
        return {}
    if event_type == "REPLY_END":
        payload: JsonObject = {}
        reason = native_payload.get("finished_reason")
        if isinstance(reason, str):
            payload["finished_reason"] = reason
        error = native_payload.get("error")
        if isinstance(error, dict):
            error_type = error.get("type")
            if isinstance(error_type, str):
                payload["error"] = {"type": error_type}
        return payload
    return {}


def _receipt_span_attributes(receipt: RuntimeReceipt) -> dict[str, AttributeValue]:
    if receipt.type not in {"REQUIRE_USER_CONFIRM", "REQUIRE_EXTERNAL_EXECUTION"}:
        return {}
    tool_calls = receipt.payload.get("tool_calls")
    tool_call_ids = tuple(str(tool_call["id"]) for tool_call in tool_calls or [] if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str))
    key = (
        "agentscope.agent.hitl_pending_tool_call_ids" if receipt.type == "REQUIRE_USER_CONFIRM" else "agentscope.agent.external_execution_pending_tool_call_ids"
    )
    return {key: tool_call_ids} if tool_call_ids else {}


def _annotate_incoming_event(value: object) -> None:
    """Annotate the continuation span without copying its result bodies."""

    if isinstance(value, UserConfirmResultEvent):
        event_type = "USER_CONFIRM_RESULT"
    elif isinstance(value, ExternalExecutionResultEvent):
        event_type = "EXTERNAL_EXECUTION_RESULT"
    else:
        return
    trace.get_current_span().set_attributes(
        {
            "agentscope.agent.incoming_event_type": event_type,
            "agentscope.agent.reply_id": value.reply_id,
        },
    )
