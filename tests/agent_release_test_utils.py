from __future__ import annotations

from pathlib import Path

from app.runtime_gateway.provisioning import RuntimeAgentBinding
from app.runtime_gateway.store import harness_digest


def install_release_activation_boundary(governance) -> None:
    """为仅验证 Git/DB 发布编排的单元测试提供已通过的 Runtime 边界结果。"""

    async def activate(*, agent_id: str, agent_version_id: str, candidate_worktree: Path) -> RuntimeAgentBinding:
        digest = harness_digest(candidate_worktree)
        source_id = f"published-test-{agent_version_id[:16]}"
        return RuntimeAgentBinding(
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            runtime_agent_id=f"runtime-{agent_version_id[:16]}",
            harness_digest=digest,
            workspace_id=f"{source_id}--v-{digest}",
            permission_mode="dont_ask",
            cwd=".",
            model_profile="default",
        )

    async def settle(_binding: RuntimeAgentBinding) -> None:
        return None

    governance.release_activator = activate
    governance.release_activation_committer = settle
    governance.release_activation_compensator = settle
