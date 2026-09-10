from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision, PermissionMode
from agentscope.tool import Bash, Read, Write
from agentscope.workspace import BubblewrapWorkspace
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware

ROOT = Path(__file__).resolve().parents[1]
BUSINESS_WORKSPACE = ROOT / "docker" / "runtime-bootstrap" / "business-agents" / "security-operations-expert" / "workspace"


def test_committed_policy_allows_only_agent_visible_workspace_paths() -> None:
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


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Bubblewrap executable is unavailable")
def test_real_bubblewrap_exposes_workspace_data_and_outputs_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def disable_gateway_bootstrap(_: BubblewrapWorkspace) -> None:
        # This contract test exercises the real AgentScope filesystem backend;
        # starting its unrelated MCP gateway would download/bootstrap packages.
        return None

    monkeypatch.setattr(BubblewrapWorkspace, "_setup_mcp_gateway", disable_gateway_bootstrap)
    host_workdir = tmp_path / "host-workdir"

    async def exercise() -> None:
        workspace = BubblewrapWorkspace(
            workspace_id="workspace-contract",
            host_workdir=str(host_workdir),
            gateway_port=None,
            extra_pip=[],
            skill_paths=[],
        )
        try:
            try:
                await workspace.initialize()
            except RuntimeError as exc:
                if "BubblewrapWorkspace bwrap smoke probe" in str(exc):
                    pytest.skip(f"Bubblewrap is installed but unavailable in this environment: {exc}")
                raise
            backend = workspace.get_backend()
            await backend.write_file("/workspace/data/input.json", b'{"alert_id":"a-1"}\n')
            await backend.write_file(
                "/workspace/outputs/security-operations-expert/result.md",
                b"result\n",
            )
            assert await backend.read_file("/workspace/data/input.json") == b'{"alert_id":"a-1"}\n'
            assert await backend.read_file("/workspace/outputs/security-operations-expert/result.md") == b"result\n"
            with pytest.raises(PermissionError, match="limited to"):
                await backend.write_file("/runtime-data/result.md", b"forbidden\n")
            probe = await backend.exec_shell(
                [
                    "sh",
                    "-c",
                    "test -d /workspace/data && test -d /workspace/outputs && test ! -e /runtime-data",
                ],
                cwd="/",
            )
            assert probe.ok(), probe.stderr.decode("utf-8", errors="replace")
        finally:
            await workspace.close()

    asyncio.run(exercise())
    assert (host_workdir / "data" / "input.json").is_file()
    assert (host_workdir / "outputs" / "security-operations-expert" / "result.md").is_file()
