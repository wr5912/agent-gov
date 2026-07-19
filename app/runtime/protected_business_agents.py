"""内置、默认与受保护业务 Agent 的单一真相源。

三种属性分别建模：内置表示空运行卷会从仓库初始化，默认表示标准接口未配置出口时选用，
受保护表示不能经在线删除接口移除。当前三个集合只有同一个成员，但它们不是同义词。
"""

from __future__ import annotations

SECURITY_OPERATIONS_EXPERT_AGENT_ID = "security-operations-expert"
DEFAULT_BUSINESS_AGENT_ID = SECURITY_OPERATIONS_EXPERT_AGENT_ID
BUILTIN_BUSINESS_AGENT_IDS = frozenset({SECURITY_OPERATIONS_EXPERT_AGENT_ID})

# 安全运营专家携带剧本执行能力与审批治理契约，其内置 Workspace 必须经仓库评审变更。
PROTECTED_BUSINESS_AGENT_IDS = frozenset({SECURITY_OPERATIONS_EXPERT_AGENT_ID})


def is_protected_business_agent(agent_id: str) -> bool:
    return agent_id in PROTECTED_BUSINESS_AGENT_IDS


def is_builtin_business_agent(agent_id: str) -> bool:
    return agent_id in BUILTIN_BUSINESS_AGENT_IDS


def is_default_business_agent(agent_id: str) -> bool:
    return agent_id == DEFAULT_BUSINESS_AGENT_ID
