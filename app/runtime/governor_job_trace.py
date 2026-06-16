from __future__ import annotations

"""治理 Agent（governor）job 的 Langfuse 富化（整改方案 §4.4 / §5.6）。

业务 Agent 聊天经 ClaudeRuntime.run/stream 富化；治理 job 经 run_profile_json 执行，
此前不富化、不可观测。本模块为治理 job 套一层 in-process Langfuse 观测，使其 trace：
  - trace name = runtime.governor.{job_type}（与业务 runtime.business_agent.* 区分）
  - sessionId  = 治理范围 id（case:/batch:，统一规则而非聊天 session_id）
  - tags       = role:governance + agent:governor + job_type:{job_type}
  - userId     = system:governor
从而在同一 Langfuse project 内按 role/agent/session 区分治理与业务、并定位治理活动。
"""

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from .json_types import JsonObject

# scope_kind -> sessionId 前缀。治理范围天然分组单元：归因按 case，批次/执行/评估/回归按 batch。
_SCOPE_SESSION_PREFIX = {
    "feedback_case": "case",
    "optimization_batch": "batch",
    "optimization_task": "batch",
    "eval_run": "batch",
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
) -> Any:
    """在治理 job 富化上下文内执行 run()；governor 缺省或 Langfuse 关闭时直接执行。"""
    enabled = bool(getattr(getattr(langfuse, "settings", None), "langfuse_enabled", False))
    if not governor or not enabled:
        return await run()
    attrs = governor_trace_attributes(
        job_type=str(governor.get("job_type") or ""),
        scope_kind=str(governor.get("scope_kind") or ""),
        scope_id=str(governor.get("scope_id") or ""),
        job_id=str(governor.get("job_id") or ""),
    )
    with langfuse.propagate_attributes(**attrs):
        with langfuse.start_observation(as_type="span", name=attrs["trace_name"], metadata=attrs["metadata"]) as root_span:
            langfuse.set_trace_attributes(root_span, **attrs)
            return await run()
