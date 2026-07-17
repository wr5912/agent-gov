from __future__ import annotations

from typing import Any


def non_stream_permission_callback() -> Any:
    """Non-streaming execution has no decision surface, so every ask fails closed."""

    from claude_agent_sdk import PermissionResultDeny

    async def deny(tool_name: str, input_data: Any, sdk_context: Any) -> Any:
        return PermissionResultDeny(message=f"工具 {tool_name} 请求人工审批，但非流式执行没有 HITL 决策面；请改用 stream=true 并开启 Claude Web HITL。")

    return deny
