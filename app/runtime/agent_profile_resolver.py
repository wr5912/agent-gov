from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.runtime.agent_profiles import (
    MAIN_AGENT_PROFILE,
    AgentRuntimeProfile,
    build_business_agent_profile,
)
from app.runtime.business_agent_workspace import initialize_business_agent_workspace
from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.settings import AppSettings
from app.runtime.state_machines import AGENT_RUNNABLE_LIFECYCLE_STATES
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def resolve_business_profile(
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    agent_id: Optional[str],
) -> Optional[AgentRuntimeProfile]:
    """把 agent_id 解析为业务 Agent profile；空 / main-agent 返回 None（运行时走 main agent）。

    只有已注册的 runnable 业务 Agent 可被运行：未知 -> 404；治理 / 非业务 Agent -> 400；
    非活跃生命周期 -> 400。运行前确保配置容器存在（幂等）。被 /api/chat、/api/chat/stream、
    /v1/chat/completions 共用，保证三处的 Agent 归属与隔离口径一致。
    """
    normalized = (agent_id or "").strip()
    if not normalized or normalized == MAIN_AGENT_PROFILE:
        return None
    agent = agent_registry_store.get_agent(normalized)
    if agent is None:
        raise NotFoundError(f"Business agent not found: {normalized}")
    if agent.category != "business":
        raise BusinessRuleViolation(f"Agent is not a runnable business agent: {normalized}")
    # AGV-020 criterion 3：archived/deprecated/draft 等非活跃 Agent 不参与新运行（仍可审计）。
    if agent.status not in AGENT_RUNNABLE_LIFECYCLE_STATES:
        raise BusinessRuleViolation(f"Agent {normalized} is {agent.status}; not available for new runs")
    workspace = Path(agent.workspace_dir)
    initialize_business_agent_workspace(workspace, agent_id=agent.agent_id, name=agent.name)
    return build_business_agent_profile(settings, agent_id=agent.agent_id, workspace_dir=workspace)
