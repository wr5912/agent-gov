"""把 AgentGov 预分配 trace_id 设为 AgentScope reply 的远端父上下文。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from agentscope.event import ExternalExecutionResultEvent, ReplyEndEvent, ReplyStartEvent, UserConfirmResultEvent
from agentscope.middleware import MiddlewareBase
from opentelemetry import context as otel_context
from opentelemetry.trace import Tracer

from .receipt_middleware import CURRENT_RUNTIME_CONTEXT, fetch_runtime_context
from .run_trace import AgentGovRunTraceRegistry
from .settings import RuntimeSettings
from .types import MiddlewareInput

try:
    _AGENTSCOPE_VERSION = version("agentscope")
except PackageNotFoundError:  # pragma: no cover - 生产镜像固定安装 AgentScope
    _AGENTSCOPE_VERSION = "unknown"


class AgentGovTraceContextMiddleware(MiddlewareBase):
    """每次 initial/HITL resume 都恢复同一个 AgentGov run trace。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        tracer: Tracer | None = None,
        trace_registry: AgentGovRunTraceRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._trace_registry = trace_registry or AgentGovRunTraceRegistry(tracer)

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: MiddlewareInput,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        async with httpx.AsyncClient(
            base_url=self._settings.agentgov_api_base_url,
            timeout=self._settings.request_timeout_seconds,
            trust_env=False,
        ) as client:
            runtime_context = await fetch_runtime_context(
                client,
                self._settings,
                agent.state.session_id,
            )
        reply_id = str(getattr(agent.state, "reply_id", "") or "pending")
        stage = self._trace_registry.start_stage(
            runtime_context,
            stage=self._run_stage(input_kwargs.get("inputs")),
            reply_id=reply_id,
            runtime_version=self._settings.runtime_version,
            agentscope_version=_AGENTSCOPE_VERSION,
        )
        generator = next_handler(**input_kwargs)
        failed = False
        try:
            while True:
                runtime_token = CURRENT_RUNTIME_CONTEXT.set(runtime_context)
                otel_token = otel_context.attach(stage.context)
                try:
                    item = await anext(generator)
                except StopAsyncIteration:
                    break
                finally:
                    otel_context.detach(otel_token)
                    CURRENT_RUNTIME_CONTEXT.reset(runtime_token)
                if isinstance(item, ReplyStartEvent):
                    stage.set_reply_id(item.reply_id)
                if isinstance(item, ReplyEndEvent):
                    finished_reason = getattr(item.finished_reason, "value", item.finished_reason)
                    stage.set_finished_reason(str(finished_reason))
                yield item
        except BaseException:
            failed = True
            raise
        finally:
            otel_token = otel_context.attach(stage.context)
            try:
                await generator.aclose()
            except BaseException:
                failed = True
                raise
            finally:
                otel_context.detach(otel_token)
                stage.end(failed=failed)

    @staticmethod
    def _run_stage(inputs: object) -> str:
        if isinstance(inputs, UserConfirmResultEvent):
            return "hitl_resume"
        if isinstance(inputs, ExternalExecutionResultEvent):
            return "external_execution_resume"
        return "initial"
