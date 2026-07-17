"""多业务 Agent 治理相关 API schema（stage-2）。

从 `schemas.py` 拆出，承载业务 Agent 创建/身份/生命周期/删除影响面/反馈归属修正/资产 provenance
等契约，避免 `schemas.py` 超出 800 行架构阈值。这些模型自包含，仅相互引用。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    agent_id: Optional[str] = None
    # 创建时基于的模板（catalog 子目录名）；缺省用 general。未知值 → 422。
    template_id: Optional[str] = Field(default=None)
    source_seed_id: Optional[str] = Field(
        default=None,
        description="Declared business-Agent seed id to copy byte-for-byte. Mutually exclusive with template_id.",
    )

    @model_validator(mode="after")
    def validate_workspace_source(self) -> AgentCreateRequest:
        if self.template_id is not None and self.source_seed_id is not None:
            raise ValueError("template_id and source_seed_id are mutually exclusive")
        return self


class BusinessAgentTemplatesResponse(BaseModel):
    """业务 Agent 创建模板 catalog（可选 template_id 列表）。"""

    templates: list[str]
    seed_agent_ids: list[str] = Field(default_factory=list)


class AgentSummaryResponse(BaseModel):
    agent_id: str
    name: str
    category: str
    workspace_dir: str
    created_at: str
    status: str = Field(default="active", description="生命周期状态：draft/active/evaluating/deprecated/archived。")
    origin: str = Field(default="user", description="来源：seed（声明式基线，禁删，去 seed 源移除）/ user（用户创建，可删除）。")
    requires_web_hitl: bool = Field(
        default=False,
        description="从 workspace project settings 的 permissions.ask 派生；为 true 时交互审批依赖 ENABLE_CLAUDE_WEB_HITL。",
    )


class AssetProvenanceImprovement(BaseModel):
    improvement_id: str
    agent_id: str
    title: str
    improvement_stage: str
    improvement_status: str
    source_feedback_refs: list[str] = Field(default_factory=list)
    change_set_ids: list[str] = Field(default_factory=list, description="该改进事项已关联的 Agent change set。")


class AssetProvenanceResponse(BaseModel):
    """某次反馈的资产关系链（AGV-022）：反馈影响了哪个 Agent、进入哪些改进事项和变更集。"""

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
    eval_runs: int = Field(default=0, description="该 Agent 归属的评估运行数（影响面提示，按 limit 截顶）。")
    change_sets: int = Field(default=0, description="该 Agent 归属的版本 change set 数（影响面提示，按 limit 截顶）。")
    releases: int = Field(default=0, description="该 Agent 归属的版本 release 数（影响面提示，按 limit 截顶）。")


class AgentDeleteResponse(BaseModel):
    deleted: AgentSummaryResponse
    impact: AgentDeletionImpact = Field(description="删除前的治理影响面提示，避免无声删除治理对象。")
