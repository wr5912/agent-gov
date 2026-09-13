from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision, PermissionMode
from agentscope.tool import Bash, Read, Write
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware

ROOT = Path(__file__).resolve().parents[1]
BUSINESS_WORKSPACE = ROOT / "docker" / "runtime-bootstrap" / "business-agents" / "security-operations-expert" / "workspace"


def test_committed_policy_allows_only_agent_visible_workspace_paths() -> None:
    """真实 Harness policy 必须显式允许工作区路径并默认拒绝宿主路径。"""

    middleware = AgentGovPolicyMiddleware(
        BUSINESS_WORKSPACE,
        tool_workdir="/workspace",
        environ={"SEC_OPS_MCP_URL": "http://host.docker.internal:58001/mcp"},
    )

    async def native(**_: object) -> PermissionDecision:
        raise AssertionError("AgentGov policy must decide the committed Harness rules")

    async def decide(tool: object, tool_input: dict[str, object]) -> PermissionDecision:
        agent = SimpleNamespace(
            state=SimpleNamespace(
                permission_context=PermissionContext(mode=PermissionMode.DEFAULT),
            ),
        )
        return await middleware.on_check_permission(
            agent,
            {"tool_call": object(), "tool": tool, "tool_input": tool_input},
            native,
        )

    allowed = (
        (Read(), {"file_path": "/workspace/data/input.json"}),
        (
            Write(),
            {
                "file_path": "/workspace/outputs/security-operations-expert/result.md",
                "content": "result",
            },
        ),
        (
            Bash(),
            {"command": "mkdir -p /workspace/outputs/security-operations-expert/case-1"},
        ),
    )
    for tool, tool_input in allowed:
        assert asyncio.run(decide(tool, tool_input)).behavior is PermissionBehavior.ALLOW

    denied = (
        (Read(), {"file_path": "/runtime-data/uploads/input.json"}),
        (
            Write(),
            {
                "file_path": "/runtime-data/outputs/security-operations-expert/result.md",
                "content": "result",
            },
        ),
        (
            Bash(),
            {"command": "mkdir -p /runtime-data/outputs/security-operations-expert/case-1"},
        ),
    )
    for tool, tool_input in denied:
        assert asyncio.run(decide(tool, tool_input)).behavior is PermissionBehavior.DENY
