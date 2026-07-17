"""受保护业务 Agent 的单一真相源。

「受保护」只表达一件事：该 Agent 的注册身份与运行态 seed 不能由在线 API 删除或覆写，
因为它的真相源在项目仓库（`docker/runtime-volume-seeds/`），必须经受保护 PR 变更。

保护与 `origin` 无关。origin 是「出生时是否来自声明 seed」的派生投影，会随运行态 seed
catalog 内容漂移；把删除权限挂在 origin 上会让保护随之漂移。因此删除保护只认本模块的
显式名单。
"""

from __future__ import annotations

SECURITY_OPERATIONS_EXPERT_AGENT_ID = "security-operations-expert"

# 安全运营专家 Agent 携带真实剧本执行能力与审批治理契约，其配置与 seed 必须留在仓库并经
# 评审变更；在线删除或在线覆写它等于让运行态绕过仓库评审。其余业务 Agent（含 main-agent）
# 均可删除——main-agent 只是出厂默认，不是不可替代的平台组件。
PROTECTED_BUSINESS_AGENT_IDS = frozenset({SECURITY_OPERATIONS_EXPERT_AGENT_ID})


def is_protected_business_agent(agent_id: str) -> bool:
    return agent_id in PROTECTED_BUSINESS_AGENT_IDS
