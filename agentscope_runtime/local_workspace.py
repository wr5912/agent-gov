"""AgentScope workspace carrying an immutable AgentGov Harness binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentgov_harness_digest import harness_content_digest as harness_digest
from agentscope.mcp import MCPClient
from agentscope.skill import LocalSkillLoader, Skill
from agentscope.workspace import BubblewrapWorkspace

from .mcp_resource_middleware import MCPResourcePolicy
from .offline_gateway import offline_gateway_env


class AgentGovLocalWorkspace(BubblewrapWorkspace):
    """Bubblewrap 隔离的可写状态；Harness 只由可信 Runtime 进程读取。"""

    def __init__(
        self,
        *,
        harness_root: Path,
        expected_digest: str,
        sandbox_env: dict[str, str] | None = None,
        default_mcps: list[MCPClient] | None = None,
        mcp_resource_policies: tuple[MCPResourcePolicy, ...] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(
            skill_paths=[],
            default_mcps=default_mcps,
            share_net=True,
            gateway_port=None,
            env={
                **(sandbox_env or {}),
                **offline_gateway_env(),
            },
            extra_pip=["agentscope==2.0.8"],
            **kwargs,
        )
        self.harness_root = str(harness_root)
        self._expected_digest = expected_digest
        self.mcp_resource_policies = mcp_resource_policies
        self._skill_loader = LocalSkillLoader(str(harness_root / "skills"), scan_subdir=True)

    async def list_skills(self, *, agent_id: str | None = None) -> list[Skill]:
        """Load governed skills from the read-only source, never a writable seed copy."""

        del agent_id
        self._validate_source_digest()
        skills = await self._skill_loader.list_skills()
        self._validate_source_digest()
        return skills

    def _validate_source_digest(self) -> None:
        if harness_digest(Path(self.harness_root)) != self._expected_digest:
            raise ValueError("Harness tree digest changed after workspace binding")
