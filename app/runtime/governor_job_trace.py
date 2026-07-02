"""治理 Agent（governor）job 的 Langfuse 富化（整改方案 §4.4 / §5.6）。

业务 Agent 聊天经 ClaudeRuntime.run/stream 富化；治理 job 经 run_profile_json 执行，
此前不富化、不可观测。本模块为治理 job 套一层 in-process Langfuse 观测，使其 trace：
  - trace name = runtime.governor.{job_type}（与业务 runtime.business_agent.* 区分）
  - sessionId  = 治理范围 id（case:/improvement:/eval:，统一规则而非聊天 session_id）
  - tags       = role:governance + agent:governor + job_type:{job_type}
  - userId     = system:governor
从而在同一 Langfuse project 内按 role/agent/session 区分治理与业务、并定位治理活动。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from .json_types import JsonObject
from .message_utils import to_plain

# scope_kind -> sessionId 前缀。治理范围天然分组单元：归因按 case，四阶段按 improvement，评估按 eval。
_SCOPE_SESSION_PREFIX = {
    "feedback_case": "case",
    "eval_run": "eval",
    "improvement": "improvement",
}


def governor_trace_attributes(*, job_type: str, scope_kind: str, scope_id: str, job_id: str) -> JsonObject:
    prefix = _SCOPE_SESSION_PREFIX.get(scope_kind) or (scope_kind or "job")
    session_id = f"{prefix}:{scope_id}" if scope_id else f"job:{job_id}"
    return {
        "user_id": "system:governor",
        "session_id": session_id,
        "metadata": {
            "job_id": job_id,
            "job_type": job_type,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
        },
        "trace_name": f"runtime.governor.{job_type}",
        "tags": ["role:governance", "agent:governor", f"job_type:{job_type}"],
    }


async def run_governor_profile_json(
    langfuse: Any,
    run: Callable[[], Awaitable[Any]],
    governor: Optional[JsonObject],
    trace_callback: Callable[[JsonObject], None] | None = None,
) -> Any:
    """在治理 job 富化上下文内执行 run()；governor 缺省或 Langfuse 关闭时直接执行。"""
    enabled = bool(getattr(getattr(langfuse, "settings", None), "langfuse_enabled", False))
    if not governor or not enabled:
        result = await run()
        _notify_trace_callback(langfuse, trace_callback)
        return result
    attrs = governor_trace_attributes(
        job_type=str(governor.get("job_type") or ""),
        scope_kind=str(governor.get("scope_kind") or ""),
        scope_id=str(governor.get("scope_id") or ""),
        job_id=str(governor.get("job_id") or ""),
    )
    with langfuse.propagate_attributes(**attrs):
        trace_input = _governor_input_payload(governor, attrs)
        with langfuse.start_observation(
            as_type="span",
            name=attrs["trace_name"],
            input=trace_input,
            metadata=attrs["metadata"],
        ) as root_span:
            langfuse.set_trace_attributes(root_span, **attrs)
            try:
                result = await run()
            except Exception as exc:
                trace_output: JsonObject = {
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                }
                _update_root_observation(langfuse, root_span, trace_input=trace_input, trace_output=trace_output)
                _notify_trace_callback(langfuse, trace_callback)
                raise
            trace_output = _governor_output_payload(result)
            _update_root_observation(langfuse, root_span, trace_input=trace_input, trace_output=trace_output)
            _notify_trace_callback(langfuse, trace_callback)
            return result


def _governor_input_payload(governor: JsonObject, attrs: JsonObject) -> JsonObject:
    raw_input = governor.get("input")
    if isinstance(raw_input, dict):
        return _json_object(raw_input)
    return _json_object({"metadata": attrs["metadata"]})


def _governor_output_payload(result: Any) -> JsonObject:
    payload = to_plain(result)
    if isinstance(payload, dict):
        return _json_object({"status": "completed", "result": payload})
    return _json_object({"status": "completed", "result": payload})


def _update_root_observation(langfuse: Any, root_span: Any, *, trace_input: JsonObject, trace_output: JsonObject) -> None:
    update = getattr(langfuse, "update_observation", None)
    if update is not None:
        update(root_span, output=trace_output)
    set_trace_io = getattr(langfuse, "set_trace_io", None)
    if set_trace_io is not None:
        set_trace_io(root_span, input=trace_input, output=trace_output)


def _json_object(value: Any) -> JsonObject:
    plain = to_plain(value)
    return plain if isinstance(plain, dict) else {"value": plain}


def _notify_trace_callback(langfuse: Any, trace_callback: Callable[[JsonObject], None] | None) -> None:
    if trace_callback is None:
        return
    trace_id, trace_url = langfuse.current_trace_ref()
    trace_callback({"trace_id": trace_id or "", "trace_url": trace_url or ""})
