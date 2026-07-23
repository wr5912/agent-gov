"""多业务 Agent 治理相关 API schema（stage-2）。

从 `schemas.py` 拆出，承载业务 Agent 身份/生命周期/删除影响面/反馈归属修正/资产 provenance
等契约，避免 `schemas.py` 超出 800 行架构阈值。这些模型自包含，仅相互引用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field

from app.runtime.protected_business_agents import (
    is_builtin_business_agent,
    is_default_business_agent,
    is_protected_business_agent,
)

if TYPE_CHECKING:
    from app.runtime.stores.agent_registry_store import AgentRegistryRecord


class AgentSummaryResponse(BaseModel):
    agent_id: str
    name: str
    category: str
    workspace_dir: str
    created_at: str
    status: str = Field(default="active", description="生命周期状态：draft/active/evaluating/deprecated/archived。")
    builtin: bool = Field(default=False, description="是否由运行卷初始化源随产品提供。")
    default: bool = Field(default=False, description="是否为标准接口未显式配置时使用的默认业务 Agent。")
    protected: bool = Field(
        default=False,
        description="受保护业务 Agent：其内置 Workspace 在项目仓库维护，只能经受保护 PR 变更，不接受在线删除。",
    )
    requires_web_hitl: bool = Field(
        default=False,
        description="从 workspace project settings 的 permissions.ask 派生；为 true 时交互审批依赖 ENABLE_CLAUDE_WEB_HITL。",
    )


class AgentStarterPromptResponse(BaseModel):
    label: str = Field(description="Welcome Card 建议任务的可见标签。")
    prompt: str = Field(description="点击建议任务后填入输入框的完整内容；前端不得自动发送。")


class AgentPresentationResponse(BaseModel):
    """业务 Agent Welcome Card 的结构化只读投影。"""

    agent_id: str = Field(description="平台注册表中的业务 Agent 身份。")
    name: str = Field(description="平台注册表中的业务 Agent 展示名称。")
    version: Optional[str] = Field(default=None, description="agent.yaml 中声明的 Agent 版本。")
    language: Optional[str] = Field(default=None, description="agent.yaml 中声明的主要语言。")
    runtime: Optional[str] = Field(default=None, description="agent.yaml 中声明的 Agent Runtime。")
    capabilities: list[str] = Field(default_factory=list, description="agent.yaml 中的机器可读能力标识。")
    summary: Optional[str] = Field(default=None, description="Welcome Card 的一句话角色摘要。")
    welcome_message: Optional[str] = Field(default=None, description="会话开始前展示的静态 Markdown 开场内容。")
    composer_placeholder: Optional[str] = Field(default=None, description="当前 Agent 的输入框占位文字。")
    starter_prompts: list[AgentStarterPromptResponse] = Field(
        default_factory=list,
        description="会话开始前可填入输入框的建议任务。",
    )
    source: Literal["agent_yaml", "registry_fallback"] = Field(
        description="补充展示信息来自 agent.yaml，或只使用平台注册表回退。",
    )


class AssetProvenanceImprovement(BaseModel):
    improvement_id: str
    agent_id: str
    title: str
    improvement_stage: str
    improvement_status: str
    source_feedback_refs: list[str] = Field(default_factory=list)
    change_set_ids: list[str] = Field(default_factory=list, description="该改进事项已关联的 Agent 待发布变更。")


class AssetProvenanceResponse(BaseModel):
    """某次反馈的资产关系链（AGV-022）：反馈影响了哪个 Agent、进入哪些改进事项和待发布变更。"""

    feedback_case_id: str
    agent_ids: list[str] = Field(default_factory=list, description="该反馈归属的 Agent（影响了哪个 Agent）。")
    improvements: list[AssetProvenanceImprovement] = Field(default_factory=list)


class AgentLifecycleTransitionRequest(BaseModel):
    status: str = Field(description="目标生命周期状态：active/evaluating/deprecated/archived（draft 仅创建态）。")


class FeedbackSignalReassignRequest(BaseModel):
    agent_id: str = Field(description="修正后的归属业务 Agent。")
    operator: str = Field(description="执行修正的操作人，用于审计。")
    reason: Optional[str] = Field(default=None, description="修正原因（可选），写入审计记录。")


class AgentDeletionImpact(BaseModel):
    runs: int = Field(description="该 Agent 归属的运行记录数（影响面提示，按 limit 截顶）。")
    feedback_signals: int = Field(description="该 Agent 归属的反馈信号数（影响面提示，按 limit 截顶）。")
    improvements: int = Field(default=0, description="该 Agent 归属的改进事项数（影响面提示，按 limit 截顶）。")
    test_runs: int = Field(default=0, description="该 Agent 归属的平台测试运行记录数（影响面提示，按 limit 截顶）。")
    change_sets: int = Field(default=0, description="该 Agent 归属的待发布变更数（影响面提示，按 limit 截顶）。")
    releases: int = Field(default=0, description="该 Agent 归属的版本 release 数（影响面提示，按 limit 截顶）。")


def agent_summary_response(record: AgentRegistryRecord) -> AgentSummaryResponse:
    """把注册表记录投影为 API 摘要。

    单一实现：`protected` 是 backend-owned 派生字段，两处各投影一次必然漂移——此前路由层与
    workspace 包服务各有一份逐字段拷贝，新增字段就要记得改两遍。
    """

    return AgentSummaryResponse(
        agent_id=record.agent_id,
        name=record.name,
        category=record.category,
        workspace_dir=record.workspace_dir,
        created_at=record.created_at,
        status=record.status,
        builtin=is_builtin_business_agent(record.agent_id),
        default=is_default_business_agent(record.agent_id),
        protected=is_protected_business_agent(record.agent_id),
        requires_web_hitl=record.requires_web_hitl,
    )


class AgentDeleteResponse(BaseModel):
    deleted: AgentSummaryResponse
    impact: AgentDeletionImpact = Field(description="删除前的治理影响面提示，避免无声删除治理对象。")
    workspace_removed: bool = Field(default=True, description="该 Agent 的运行态目录（workspace/claude-root/version）是否已确认删除。")
    cleanup_complete: bool = Field(default=True, description="磁盘清理是否完整。为 false 时注册表已删除但存在磁盘残留，同 id 重建会被安全供给流程拦住。")
