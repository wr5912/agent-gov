from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.runtime.agent_profiles import (
    MAIN_AGENT_PROFILE,
    AgentRuntimeProfile,
    build_business_agent_profile,
)
from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.settings import AppSettings
from app.runtime.state_machines import AGENT_RUNNABLE_LIFECYCLE_STATES
from app.runtime.stores.agent_registry_store import AgentRegistryStore

# 未指定 agent_id 时的出厂默认。它只是某个普通业务 Agent 的 id，不是平台组件：main-agent
# 可以被删除，此时未指定 agent_id 的请求得到明确 404，而不是跑在一个已不存在的 Agent 上。
FACTORY_DEFAULT_AGENT_ID = MAIN_AGENT_PROFILE


def resolve_business_profile(
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    agent_id: Optional[str],
) -> AgentRuntimeProfile:
    """把 agent_id 解析为业务 Agent profile。

    契约：**永不返回 None**。空 agent_id 解析为出厂默认 Agent，并与任何其他 agent_id 一样
    走注册表校验——main-agent 没有豁免。因此删除 main 之后，未指定 agent_id 的请求得到
    404「Business agent not found」，而不是 KeyError 或静默跑在幽灵 profile 上。

    只有已注册的 runnable 业务 Agent 可被运行：未知 -> 404；治理 / 非业务 Agent -> 400；
    非活跃生命周期 -> 400。

    本函数是**只读投影**：读注册表与磁盘事实，不写入、不补齐、不复活 workspace 文件。
    workspace 物化只发生在 Agent 创建路径与 bootstrap 播种中——turn 路径上的写入会在下一轮
    把用户或导入有意删除的文件复活，并绕过维护栅栏。
    """
    normalized = (agent_id or "").strip() or FACTORY_DEFAULT_AGENT_ID
    agent = agent_registry_store.get_agent(normalized)
    if agent is None:
        raise NotFoundError(f"Business agent not found: {normalized}")
    if agent.category != "business":
        raise BusinessRuleViolation(f"Agent is not a runnable business agent: {normalized}")
    # AGV-020 criterion 3：archived/deprecated/draft 等非活跃 Agent 不参与新运行（仍可审计）。
    if agent.status not in AGENT_RUNNABLE_LIFECYCLE_STATES:
        raise BusinessRuleViolation(f"Agent {normalized} is {agent.status}; not available for new runs")
    return build_business_agent_profile(settings, agent_id=agent.agent_id, workspace_dir=Path(agent.workspace_dir))
