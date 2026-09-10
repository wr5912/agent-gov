"""AgentGov 侧的 Agent 身份与 Harness profile。

该模块只描述 Git 中的受治理资产，不保存 AgentScope Session/Message/State。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from .agent_paths import InvalidAgentId, business_agent_layout, business_agents_root, validate_agent_id
from .settings import AppSettings

AgentRole = Literal["business-agent", "governor"]
AgentCategory = Literal["business", "governance"]
BUSINESS_AGENT_ROLE: AgentRole = "business-agent"
GOVERNOR_PROFILE = "governor"


def agent_category(role: AgentRole) -> AgentCategory:
    return "governance" if role == GOVERNOR_PROFILE else "business"


@dataclass(frozen=True)
class AgentRuntimeProfile:
    """AgentGov 对一个不可变 Harness 源目录的最小描述。"""

    name: str
    agent_id: str
    role: AgentRole
    workspace_dir: Path
    data_dir: Path
    langfuse_observation_name: str
    max_runtime_seconds: int = 300
    max_output_bytes: int = 2_000_000

    @property
    def category(self) -> AgentCategory:
        return agent_category(self.role)


def read_requires_human_confirmation(workspace_dir: Path) -> bool:
    """从唯一 ``agent.yaml`` 读取是否声明逐次人工确认。

    这是展示用观测值；真正的工具准入与审批仍由 AgentScope 原生事件和 AgentGov
    pending-call 校验共同执行。
    """

    path = workspace_dir / "agent.yaml"
    if not path.is_file() or path.is_symlink():
        return False
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return False
    if not isinstance(loaded, dict):
        return False
    session = loaded.get("session")
    if not isinstance(session, dict):
        return False
    return session.get("permission_mode") in {"default", "explore", "accept_edits"}


def build_profiles(settings: AppSettings) -> dict[str, AgentRuntimeProfile]:
    return {
        GOVERNOR_PROFILE: AgentRuntimeProfile(
            name=GOVERNOR_PROFILE,
            agent_id=GOVERNOR_PROFILE,
            role=GOVERNOR_PROFILE,
            workspace_dir=settings.governor_workspace_dir,
            data_dir=settings.data_dir,
            langfuse_observation_name="runtime.governor",
            max_runtime_seconds=settings.governance_agent_timeout_seconds,
        )
    }


def discover_business_agents(settings: AppSettings) -> list[AgentRuntimeProfile]:
    root = business_agents_root(settings.data_dir)
    discovered: list[AgentRuntimeProfile] = []
    if root.is_symlink() or not root.is_dir():
        return discovered
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            agent_id = validate_agent_id(child.name)
        except InvalidAgentId:
            continue
        workspace = business_agent_layout(settings.data_dir, agent_id).workspace
        if workspace.is_symlink() or not workspace.is_dir():
            continue
        discovered.append(build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=workspace))
    return discovered


def build_business_agent_profile(
    settings: AppSettings,
    *,
    agent_id: str,
    workspace_dir: Path,
) -> AgentRuntimeProfile:
    return AgentRuntimeProfile(
        name=agent_id,
        agent_id=agent_id,
        role=BUSINESS_AGENT_ROLE,
        workspace_dir=workspace_dir,
        data_dir=settings.data_dir,
        langfuse_observation_name=f"runtime.business_agent.{agent_id}",
    )


def candidate_profile(
    settings: AppSettings,
    *,
    agent_id: str,
    workspace_dir: Path,
    candidate_id: str,
) -> AgentRuntimeProfile:
    return AgentRuntimeProfile(
        name=f"{agent_id}-candidate-{candidate_id}",
        agent_id=agent_id,
        role=BUSINESS_AGENT_ROLE,
        workspace_dir=workspace_dir,
        data_dir=settings.data_dir,
        langfuse_observation_name=f"runtime.candidate.{agent_id}",
        max_runtime_seconds=settings.agent_test_run_timeout_seconds,
    )
