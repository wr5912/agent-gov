from __future__ import annotations

from typing import Any

from app.runtime.response_disposition_control import (
    TrustedResponseDispositionContext,
    permission_denial_reason,
)


def runtime_response_disposition(req: object) -> TrustedResponseDispositionContext | None:
    value = getattr(req, "response_disposition", None)
    return value if isinstance(value, TrustedResponseDispositionContext) else None


def non_stream_permission_callback(
    profile_name: str,
    response_disposition: TrustedResponseDispositionContext | None,
) -> Any:
    """Non-streaming execution has no decision surface, so every ask fails closed."""

    from claude_agent_sdk import PermissionResultDeny

    async def deny(tool_name: str, input_data: Any, sdk_context: Any) -> Any:
        reason = permission_denial_reason(profile_name, tool_name, response_disposition)
        return PermissionResultDeny(
            message=reason or (f"工具 {tool_name} 请求人工审批，但非流式执行没有 HITL 决策面；请改用 stream=true 并开启 Claude Web HITL。")
        )

    return deny
