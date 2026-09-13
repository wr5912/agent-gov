"""将 AgentScope 原生 AgentEvent 以签名回执投影给 AgentGov。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
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
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .context_registry import RuntimeContext, bind_reply_context
from .run_trace import AgentGovRunTraceRegistry
from .settings import RuntimeSettings
from .signing import signed_headers
from .types import JsonObject, MiddlewareInput

logger = logging.getLogger(__name__)


class RuntimeReceipt(BaseModel):
    """AgentScope 原生事件的 AgentGov HTTP 边界回执。"""

    model_config = ConfigDict(extra="forbid")

    receipt_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    reply_id: str | None
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    type: str = Field(min_length=1, max_length=64)
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
_TERMINAL_ACK_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_TOOL_CALL_STATES = frozenset({"pending", "asking", "allowed", "submitted", "finished"})

try:
    _AGENTSCOPE_VERSION = version("agentscope")
except PackageNotFoundError:  # pragma: no cover - 生产镜像固定安装 AgentScope
    _AGENTSCOPE_VERSION = "unknown"


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


def _is_retryable_receipt_error(error: Exception) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    if not isinstance(error, httpx.HTTPStatusError):
        return False
    status_code = error.response.status_code
    return status_code >= 500 or status_code in {408, 425, 429}


class AgentGovReceiptFlushError(RuntimeError):
    """Runtime 在关闭预算内未能获得全部控制回执 ACK。"""


class AgentGovReceiptDeliveryError(RuntimeError):
    """至少一条已调度控制回执永久失败且尚未成功重放。"""


class AgentGovReceiptDispatcher:
    """跟踪控制回执直到 AgentGov 确认，调用方取消不取消投递。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        trace_registry: AgentGovRunTraceRegistry,
    ) -> None:
        self._settings = settings
        self._trace_registry = trace_registry
        self._tasks: dict[str, asyncio.Task[RuntimeReceiptAck]] = {}
        self._failures: dict[str, AgentGovReceiptDeliveryError] = {}
        self._closing = False

    def schedule(
        self,
        receipt: RuntimeReceipt,
        context: RuntimeContext,
    ) -> asyncio.Task[RuntimeReceiptAck]:
        """按稳定 receipt_id 合并同一进程内的并发投递。"""

        existing = self._tasks.get(receipt.receipt_id)
        if existing is not None and not existing.done():
            return existing
        if self._closing:
            raise RuntimeError("AgentGov control receipt dispatcher is closing")
        task = asyncio.create_task(
            self._deliver_until_acknowledged(
                receipt,
                context,
            ),
            name=f"agentgov-control-receipt-{receipt.event_id[:12]}",
        )
        self._tasks[receipt.receipt_id] = task
        task.add_done_callback(
            lambda completed, scheduled_receipt=receipt: self._receipt_done(
                scheduled_receipt,
                completed,
            ),
        )
        return task

    async def deliver(
        self,
        receipt: RuntimeReceipt,
        context: RuntimeContext,
    ) -> RuntimeReceiptAck:
        """等待确认但隔离调用协程取消，确保已调度回执继续投递。"""

        return await asyncio.shield(
            self.schedule(receipt, context),
        )

    async def aclose(self) -> None:
        """在服务关闭预算内清空回执；超时回执显式记录后取消。"""

        self._closing = True
        scheduled = tuple(self._tasks.items())
        tasks = tuple(task for _, task in scheduled if not task.done())
        pending: set[asyncio.Task[RuntimeReceiptAck]] = set()
        flush_error: AgentGovReceiptFlushError | None = None
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=self._settings.receipt_flush_timeout_seconds,
            )
        if pending:
            pending_identities = sorted(task.get_name() for task in pending)
            logger.error(
                "AgentGov control receipt flush deadline expired; cancelling pending_count=%d tasks=%s",
                len(pending),
                pending_identities,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            flush_error = AgentGovReceiptFlushError(
                f"AgentGov control receipt flush failed for {len(pending)} pending task(s)",
            )
        for receipt_id, task in scheduled:
            self._remember_failure(receipt_id, task)
        errors: list[BaseException] = list(self._failures.values())
        self._failures.clear()
        if flush_error is not None:
            errors.append(flush_error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(
                "AgentGov control receipt shutdown failed",
                errors,
            )

    async def _deliver_until_acknowledged(
        self,
        receipt: RuntimeReceipt,
        context: RuntimeContext,
    ) -> RuntimeReceiptAck:
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(
                    base_url=self._settings.agentgov_api_base_url,
                    timeout=self._settings.request_timeout_seconds,
                    trust_env=False,
                ) as client:
                    acknowledgement = await post_runtime_receipt(
                        client,
                        self._settings,
                        receipt,
                    )
                self._finish_trace(
                    context,
                    acknowledgement,
                )
                return acknowledgement
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not _is_retryable_receipt_error(error):
                    raise
                attempt += 1
                self._log_retry(receipt, attempt, error)
                await asyncio.sleep(self._retry_delay(attempt))

    def _finish_trace(
        self,
        context: RuntimeContext,
        acknowledgement: RuntimeReceiptAck,
    ) -> None:
        if acknowledgement.run_id != context.run_id:
            raise RuntimeError("AgentGov receipt acknowledgement changed run identity")
        if acknowledgement.status not in _TERMINAL_ACK_STATUSES:
            return
        terminal_reason = acknowledgement.terminal_reason or acknowledgement.status
        self._trace_registry.finish_run(
            context,
            terminal_reason=terminal_reason,
            failed=acknowledgement.status != "succeeded",
            runtime_version=self._settings.runtime_version,
            agentscope_version=_AGENTSCOPE_VERSION,
        )

    def _retry_delay(self, attempt: int) -> float:
        exponent = min(attempt - 1, self._settings.receipt_retry_attempts - 1)
        return self._settings.receipt_retry_backoff_seconds * (2**exponent)

    def _log_retry(
        self,
        receipt: RuntimeReceipt,
        attempt: int,
        error: Exception,
    ) -> None:
        if attempt != 1 and attempt % self._settings.receipt_retry_attempts != 0:
            return
        status_code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        logger.warning(
            "AgentGov control receipt delivery deferred: type=%s run=%s session=%s event=%s attempt=%d error_type=%s status=%s",
            receipt.type,
            receipt.run_id,
            receipt.session_id,
            receipt.event_id,
            attempt,
            type(error).__name__,
            status_code,
        )

    def _receipt_done(
        self,
        receipt: RuntimeReceipt,
        task: asyncio.Task[RuntimeReceiptAck],
    ) -> None:
        receipt_id = receipt.receipt_id
        if self._tasks.get(receipt_id) is task:
            self._tasks.pop(receipt_id, None)
        self._remember_failure(receipt_id, task, receipt=receipt)

    def _remember_failure(
        self,
        receipt_id: str,
        task: asyncio.Task[RuntimeReceiptAck],
        *,
        receipt: RuntimeReceipt | None = None,
    ) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self._failures.pop(receipt_id, None)
            return
        if receipt_id in self._failures:
            return
        self._failures[receipt_id] = AgentGovReceiptDeliveryError(
            f"AgentGov control receipt delivery failed: task={task.get_name()}",
        )
        logger.error(
            "AgentGov control receipt delivery failed permanently: task=%s type=%s error_type=%s",
            task.get_name(),
            receipt.type if receipt is not None else "unknown",
            type(error).__name__,
        )


class AgentGovReceiptLifespanMiddleware:
    """按 persistence -> control -> trace -> provider 顺序收口生命周期。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        dispatcher: AgentGovReceiptDispatcher,
        trace_registry: AgentGovRunTraceRegistry,
        provider_shutdown: Callable[[], None] | None = None,
    ) -> None:
        self.app = app
        self._dispatcher = dispatcher
        self._trace_registry = trace_registry
        self._provider_shutdown = provider_shutdown

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        shutdown_message: Message | None = None

        async def defer_shutdown(message: Message) -> None:
            nonlocal shutdown_message
            if message["type"] in {
                "lifespan.startup.failed",
                "lifespan.shutdown.complete",
                "lifespan.shutdown.failed",
            }:
                if shutdown_message is not None:
                    raise RuntimeError("Runtime lifespan emitted more than one shutdown result")
                shutdown_message = message
                return
            await send(message)

        inner_error: BaseException | None = None
        try:
            await self.app(scope, receive, defer_shutdown)
        except BaseException as error:
            inner_error = error

        shutdown = asyncio.create_task(
            self._close_receipts_and_traces(),
            name="agentgov-control-receipt-shutdown",
        )
        cancellation: asyncio.CancelledError | None = None
        while not shutdown.done():
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError as error:
                # ASGI server 取消 lifespan 时不把取消向已启动的有界 flush 传播。
                if not shutdown.done():
                    cancellation = error
            except BaseException:
                # shutdown task 的异常在下面统一聚合并转换为 lifespan.failed。
                pass

        cleanup_error: BaseException | None = None
        if shutdown.cancelled():
            cleanup_error = asyncio.CancelledError()
        else:
            cleanup_error = shutdown.exception()

        terminal_errors: list[BaseException] = []
        if inner_error is not None:
            terminal_errors.append(inner_error)
            self._log_shutdown_stage_failure("persistence", inner_error)
        if cleanup_error is not None:
            if isinstance(cleanup_error, BaseExceptionGroup):
                terminal_errors.extend(cleanup_error.exceptions)
            else:
                terminal_errors.append(cleanup_error)
        if cancellation is not None:
            terminal_errors.append(cancellation)
            self._log_shutdown_stage_failure("lifespan", cancellation)
        if shutdown_message is not None:
            if terminal_errors:
                failed_message_type = "lifespan.shutdown.failed"
                if shutdown_message["type"] == "lifespan.startup.failed":
                    failed_message_type = "lifespan.startup.failed"
                shutdown_message = {
                    "type": failed_message_type,
                    "message": "AgentGov Runtime ordered shutdown failed",
                }
            await send(shutdown_message)

        if len(terminal_errors) == 1:
            raise terminal_errors[0]
        if terminal_errors:
            raise BaseExceptionGroup(
                "AgentGov Runtime ordered shutdown failed",
                terminal_errors,
            )

    async def _close_receipts_and_traces(self) -> None:
        errors: list[BaseException] = []
        try:
            await self._dispatcher.aclose()
        except BaseException as error:
            errors.append(error)
            self._log_shutdown_stage_failure("control", error)
        try:
            self._trace_registry.close_all()
        except BaseException as error:
            errors.append(error)
            self._log_shutdown_stage_failure("trace", error)
        if self._provider_shutdown is not None:
            try:
                await asyncio.to_thread(self._provider_shutdown)
            except BaseException as error:
                errors.append(error)
                self._log_shutdown_stage_failure("provider", error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("AgentGov Runtime shutdown failed", errors)

    @staticmethod
    def _log_shutdown_stage_failure(stage: str, error: BaseException) -> None:
        logger.error(
            "AgentGov Runtime shutdown stage failed: stage=%s error_type=%s",
            stage,
            type(error).__name__,
        )


class AgentGovReceiptMiddleware(MiddlewareBase):
    """Fail closed when a run lacks context or an event cannot be receipted."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        receipt_dispatcher: AgentGovReceiptDispatcher,
    ) -> None:
        self._settings = settings
        self._receipt_dispatcher = receipt_dispatcher

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: MiddlewareInput,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """Resolve one run context, then receipt every full native event."""

        _annotate_incoming_event(input_kwargs.get("inputs"))
        context = CURRENT_RUNTIME_CONTEXT.get()
        if context is None:
            async with httpx.AsyncClient(
                base_url=self._settings.agentgov_api_base_url,
                timeout=self._settings.request_timeout_seconds,
                trust_env=False,
            ) as client:
                context = await fetch_runtime_context(
                    client,
                    self._settings,
                    agent.state.session_id,
                )
        saw_reply_end = False
        reply_open = False
        try:
            async for item in next_handler(**input_kwargs):
                if isinstance(item, EventBase) and item.type in _CONTROL_RECEIPT_TYPES:
                    if item.type == "REPLY_START":
                        reply_open = True
                    elif item.type == "REPLY_END":
                        # AgentScope 已生成 REPLY_END 就不再属于 pre-reply；
                        # 后续控制 HTTP 取消不能改写这一事实。
                        saw_reply_end = True
                        reply_open = False
                    receipt = self._build_receipt(context, agent, item)
                    if receipt.reply_id is not None:
                        bind_reply_context(context, receipt.reply_id)
                    task = self._schedule_receipt(context, receipt)
                    await asyncio.shield(task)
                yield item
        except BaseException:
            if reply_open or not saw_reply_end:
                try:
                    self._schedule_interrupted_receipt(context)
                except Exception as error:
                    logger.error(
                        "AgentGov exceptional interruption receipt could not be scheduled: run=%s session=%s error_type=%s",
                        context.run_id,
                        context.session_id,
                        type(error).__name__,
                    )
            raise

    def _schedule_interrupted_receipt(self, context: RuntimeContext) -> None:
        receipt = self._build_interrupted_receipt(context)
        self._schedule_receipt(context, receipt)

    def _schedule_receipt(
        self,
        context: RuntimeContext,
        receipt: RuntimeReceipt,
    ) -> asyncio.Task[RuntimeReceiptAck]:
        return self._receipt_dispatcher.schedule(
            receipt,
            context,
        )

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: MiddlewareInput,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """把无正文的工具终态身份写入当前 execute_tool span。"""

        async for item in next_handler(**input_kwargs):
            if isinstance(item, ToolResultEndEvent):
                annotate_tool_result(str(agent.state.session_id), item)
            yield item

    @staticmethod
    def _build_receipt(
        context: RuntimeContext,
        agent: Any,
        event: EventBase,
    ) -> RuntimeReceipt:
        return build_runtime_receipt(
            context,
            event,
            fallback_reply_id=str(agent.state.reply_id),
        )

    @staticmethod
    def _build_interrupted_receipt(context: RuntimeContext) -> RuntimeReceipt:
        return build_interrupted_receipt(context)


def build_runtime_receipt(
    context: RuntimeContext,
    event: EventBase,
    *,
    fallback_reply_id: str,
) -> RuntimeReceipt:
    """从 AgentScope 原生事件构建无正文、可幂等的控制回执。"""

    native_payload = event.model_dump(mode="json")
    event_id = str(native_payload["id"])
    reply_id = str(native_payload.get("reply_id") or fallback_reply_id)
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


def build_interrupted_receipt(context: RuntimeContext) -> RuntimeReceipt:
    """构建不携带 reply、prompt、output 或 tool 参数的中断证据。"""

    current_trace_id = _current_trace_id()
    if current_trace_id is not None and current_trace_id != context.trace_id:
        raise ValueError("active OpenTelemetry trace does not match Runtime context")
    identity = f"RUN_INTERRUPTED\n{context.run_id}\n{context.session_id}"
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    receipt_id = hashlib.sha256(
        f"{context.run_id}\n{context.session_id}\n{event_id}".encode(),
    ).hexdigest()
    receipt = RuntimeReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        run_id=context.run_id,
        session_id=context.session_id,
        reply_id=None,
        trace_id=context.trace_id,
        type="RUN_INTERRUPTED",
        payload={},
    )
    trace.get_current_span().set_attributes(
        {
            "agentgov.run.id": context.run_id,
            "agentscope.session.id": context.session_id,
            "agentgov.event.id": event_id,
            "agentgov.event.type": receipt.type,
            "agentgov.receipt.id": receipt_id,
        },
    )
    return receipt


def annotate_tool_result(session_id: str, event: ToolResultEndEvent) -> None:
    state = getattr(event.state, "value", event.state)
    trace.get_current_span().set_attributes(
        {
            "agentscope.session.id": session_id,
            "agentscope.agent.reply_id": event.reply_id,
            "agentscope.tool.result.state": str(state),
            "gen_ai.tool.call.id": event.tool_call_id,
        },
    )


def _receipt_payload(native_payload: JsonObject) -> JsonObject:
    """只投影控制面状态机所需字段，禁止回执携带消息/工具结果正文。"""

    event_type = native_payload.get("type")
    if event_type in {"REQUIRE_USER_CONFIRM", "REQUIRE_EXTERNAL_EXECUTION"}:
        tool_calls = native_payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            return {}
        return {"tool_calls": _tool_call_fingerprints(tool_calls)}
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


def _tool_call_fingerprints(tool_calls: list[object]) -> list[JsonObject]:
    fingerprints: list[JsonObject] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            raise ValueError("AgentScope HITL tool call must be an object")
        fingerprints.append(_tool_call_fingerprint(tool_call))
    return fingerprints


def _tool_call_fingerprint(tool_call: JsonObject) -> JsonObject:
    """投影完整 canonical ToolCall 的摘要，不泄露 input 或 suggested_rules。"""

    tool_call_id = tool_call.get("id")
    tool_call_name = tool_call.get("name")
    tool_call_state = tool_call.get("state")
    if (
        not isinstance(tool_call_id, str)
        or not tool_call_id
        or not isinstance(tool_call_name, str)
        or not tool_call_name
        or tool_call_state not in _TOOL_CALL_STATES
    ):
        raise ValueError("AgentScope HITL tool call is missing stable identity or state")
    canonical = json.dumps(
        tool_call,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "tool_call_id": tool_call_id,
        "tool_call_name": tool_call_name,
        "tool_call_state": tool_call_state,
        "tool_call_utf8_length": len(canonical),
        "tool_call_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _receipt_span_attributes(receipt: RuntimeReceipt) -> dict[str, AttributeValue]:
    if receipt.type not in {"REQUIRE_USER_CONFIRM", "REQUIRE_EXTERNAL_EXECUTION"}:
        return {}
    tool_calls = receipt.payload.get("tool_calls")
    tool_call_ids = tuple(
        str(tool_call["tool_call_id"]) for tool_call in tool_calls or [] if isinstance(tool_call, dict) and isinstance(tool_call.get("tool_call_id"), str)
    )
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
