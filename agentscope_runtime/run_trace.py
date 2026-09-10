"""一个 AgentGov run 对应一个长生命周期 OpenTelemetry root span。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Span, Status, StatusCode, Tracer, set_span_in_context

_DESIRED_TRACE_ID: ContextVar[int | None] = ContextVar(
    "agentgov_desired_trace_id",
    default=None,
)
_ENDED_RUN_CACHE_SIZE = 4096


class RunTraceContext(Protocol):
    run_id: str
    session_id: str
    agent_id: str
    agent_version_id: str
    runtime_agent_id: str
    harness_digest: str
    trace_id: str


class AgentGovTraceIdGenerator(RandomIdGenerator):
    """只在创建受管 run root 时使用控制面预分配的 trace_id。"""

    def generate_trace_id(self) -> int:
        desired = _DESIRED_TRACE_ID.get()
        return desired if desired is not None else super().generate_trace_id()


@dataclass
class _RunRoot:
    trace_id: str
    span: Span
    context: Context


@dataclass
class RunTraceStage:
    """一个 initial/HITL/subagent reply stage；root 不随 stage 结束。"""

    span: Span
    context: Context

    def set_reply_id(self, reply_id: str) -> None:
        self.span.set_attribute("agentscope.agent.reply_id", reply_id)

    def set_finished_reason(self, reason: str) -> None:
        self.span.set_attribute("agentgov.run.finished_reason", reason)

    def end(self, *, failed: bool) -> None:
        self.span.set_status(Status(StatusCode.ERROR if failed else StatusCode.OK))
        self.span.end()


class AgentGovRunTraceRegistry:
    """进程内持有尚未终态的 root；HITL resume 与 Team worker 共用它。"""

    def __init__(self, tracer: Tracer | None = None) -> None:
        self._tracer = tracer or trace.get_tracer(__name__)
        self._roots: dict[str, _RunRoot] = {}
        self._ended: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.RLock()

    def start_stage(
        self,
        runtime_context: RunTraceContext,
        *,
        stage: str,
        reply_id: str,
        runtime_version: str,
        agentscope_version: str,
    ) -> RunTraceStage:
        root = self._root(
            runtime_context,
            runtime_version=runtime_version,
            agentscope_version=agentscope_version,
        )
        span = self._tracer.start_span(
            "agentgov.run.stage",
            context=root.context,
            attributes={
                "agentgov.run.id": runtime_context.run_id,
                "agentgov.run.stage": stage,
                "agentgov.agent.id": runtime_context.agent_id,
                "agentgov.agent.version_id": runtime_context.agent_version_id,
                "agentgov.harness.digest": runtime_context.harness_digest,
                "agentgov.runtime.version": runtime_version,
                "agentscope.agent.id": runtime_context.runtime_agent_id,
                "agentscope.runtime.version": agentscope_version,
                "agentscope.session.id": runtime_context.session_id,
                "agentscope.agent.reply_id": reply_id,
            },
        )
        return RunTraceStage(span=span, context=set_span_in_context(span))

    def finish_run(
        self,
        runtime_context: RunTraceContext,
        *,
        terminal_reason: str,
        failed: bool,
        runtime_version: str,
        agentscope_version: str,
    ) -> None:
        """收到控制面 terminal 响应后才结束 root；重复确认幂等。"""

        with self._lock:
            if runtime_context.run_id in self._ended:
                self._ended.move_to_end(runtime_context.run_id)
                return
            root = self._roots.pop(runtime_context.run_id, None)
            if root is None:
                root = self._create_root(
                    runtime_context,
                    runtime_version=runtime_version,
                    agentscope_version=agentscope_version,
                )
            if root.trace_id != runtime_context.trace_id:
                raise ValueError("AgentGov run cannot finish under another trace_id")
            root.span.set_attribute("agentgov.run.finished_reason", terminal_reason)
            root.span.set_status(Status(StatusCode.ERROR if failed else StatusCode.OK))
            root.span.end()
            self._remember_ended(runtime_context.run_id)

    def close_all(self) -> None:
        """Runtime 退出时关闭未终态 root；控制面会把对应 run 标为 interrupted。"""

        with self._lock:
            roots = tuple(self._roots.items())
            self._roots.clear()
            for run_id, root in roots:
                root.span.set_attribute("agentgov.run.finished_reason", "interrupted")
                root.span.set_status(Status(StatusCode.ERROR))
                root.span.end()
                self._remember_ended(run_id)

    def _root(
        self,
        runtime_context: RunTraceContext,
        *,
        runtime_version: str,
        agentscope_version: str,
    ) -> _RunRoot:
        with self._lock:
            if runtime_context.run_id in self._ended:
                raise ValueError("Terminal AgentGov run cannot create a new trace stage")
            existing = self._roots.get(runtime_context.run_id)
            if existing is not None:
                if existing.trace_id != runtime_context.trace_id:
                    raise ValueError("AgentGov run cannot be rebound to another trace_id")
                return existing
            root = self._create_root(
                runtime_context,
                runtime_version=runtime_version,
                agentscope_version=agentscope_version,
            )
            self._roots[runtime_context.run_id] = root
            return root

    def _create_root(
        self,
        runtime_context: RunTraceContext,
        *,
        runtime_version: str,
        agentscope_version: str,
    ) -> _RunRoot:
        desired = int(runtime_context.trace_id, 16)
        if desired == 0:
            raise ValueError("AgentGov trace_id must be non-zero")
        token = _DESIRED_TRACE_ID.set(desired)
        try:
            span = self._tracer.start_span(
                "agentgov.run",
                context=Context(),
                attributes={
                    "agentgov.run.id": runtime_context.run_id,
                    "agentgov.agent.id": runtime_context.agent_id,
                    "agentgov.agent.version_id": runtime_context.agent_version_id,
                    "agentgov.harness.digest": runtime_context.harness_digest,
                    "agentgov.runtime.version": runtime_version,
                    "agentscope.agent.id": runtime_context.runtime_agent_id,
                    "agentscope.runtime.version": agentscope_version,
                    "agentscope.session.id": runtime_context.session_id,
                },
            )
        finally:
            _DESIRED_TRACE_ID.reset(token)
        actual = span.get_span_context()
        if actual.is_valid and actual.trace_id != desired:
            span.end()
            raise RuntimeError("OpenTelemetry provider ignored the governed trace_id")
        return _RunRoot(
            trace_id=runtime_context.trace_id,
            span=span,
            context=set_span_in_context(span),
        )

    def _remember_ended(self, run_id: str) -> None:
        self._ended[run_id] = None
        self._ended.move_to_end(run_id)
        while len(self._ended) > _ENDED_RUN_CACHE_SIZE:
            self._ended.popitem(last=False)
