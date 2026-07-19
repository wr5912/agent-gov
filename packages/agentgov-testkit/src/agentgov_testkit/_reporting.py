from __future__ import annotations

from threading import RLock

from .result import AgentInvocation, JsonObject

_LOCK = RLock()
_INVOCATIONS: list[JsonObject] = []


def clear_invocations() -> None:
    with _LOCK:
        _INVOCATIONS.clear()


def record_invocation(result: AgentInvocation) -> None:
    with _LOCK:
        _INVOCATIONS.append(
            {
                "run_id": result.run_id,
                "session_id": result.session_id,
                "agent_version_id": result.agent_version_id,
                "langfuse_trace_id": result.langfuse_trace_id,
                "langfuse_trace_url": result.langfuse_trace_url,
                "errors": list(result.errors),
            }
        )


def invocation_records() -> list[JsonObject]:
    with _LOCK:
        return [dict(item) for item in _INVOCATIONS]
