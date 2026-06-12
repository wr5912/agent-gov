"""多业务 Agent 治理相关 API schema（stage-2）。

从 `schemas.py` 拆出，承载业务 Agent 创建/身份/生命周期/删除影响面/反馈归属修正/资产 provenance
等契约，避免 `schemas.py` 超出 800 行架构阈值。这些模型自包含，仅相互引用。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AgentCreateRequest(BaseModel):
    name: str
    agent_id: Optional[str] = None


class AgentSummaryResponse(BaseModel):
    agent_id: str
    name: str
    category: str
    workspace_dir: str
    created_at: str
    status: str = Field(default="active", description="生命周期状态：draft/active/evaluating/deprecated/archived。")


class AssetProvenanceTask(BaseModel):
    optimization_task_id: str
    status: Optional[str] = None
    target_paths: list[str] = Field(default_factory=list, description="本次优化改动的资产路径（改了哪些资产）。")
    eval_case_ids: list[str] = Field(default_factory=list)
    latest_change_set_id: Optional[str] = None
    applied_agent_version_id: Optional[str] = Field(default=None, description="改动进入的 Agent 版本（进入哪个版本）。")


class AssetProvenanceResponse(BaseModel):
    """某次反馈的资产关系链（AGV-022）：反馈影响了哪个 Agent、改了哪些资产、进入哪个版本。"""

    feedback_case_id: str
    agent_ids: list[str] = Field(default_factory=list, description="该反馈归属的 Agent（影响了哪个 Agent）。")
    optimization_tasks: list[AssetProvenanceTask] = Field(default_factory=list)


class AgentLifecycleTransitionRequest(BaseModel):
    status: str = Field(description="目标生命周期状态：active/evaluating/deprecated/archived（draft 仅创建态）。")


class FeedbackSignalReassignRequest(BaseModel):
    agent_id: str = Field(description="修正后的归属业务 Agent。")
    operator: str = Field(description="执行修正的操作人，用于审计。")
    reason: Optional[str] = Field(default=None, description="修正原因（可选），写入审计记录。")


class AgentDeletionImpact(BaseModel):
    runs: int = Field(description="该 Agent 归属的运行记录数（影响面提示，按 limit 截顶）。")
    feedback_signals: int = Field(description="该 Agent 归属的反馈信号数（影响面提示，按 limit 截顶）。")
    optimization_tasks: int = Field(default=0, description="该 Agent 归属的优化任务数（影响面提示，按 limit 截顶）。")
    eval_runs: int = Field(default=0, description="该 Agent 归属的评估运行数（影响面提示，按 limit 截顶）。")
    change_sets: int = Field(default=0, description="该 Agent 归属的版本 change set 数（影响面提示，按 limit 截顶）。")
    releases: int = Field(default=0, description="该 Agent 归属的版本 release 数（影响面提示，按 limit 截顶）。")


class AgentDeleteResponse(BaseModel):
    deleted: AgentSummaryResponse
    impact: AgentDeletionImpact = Field(description="删除前的治理影响面提示，避免无声删除治理对象。")


class ScenarioPackCreateRequest(BaseModel):
    name: str
    business_goal: str = Field(default="", description="场景包的业务目标。")
    scope: str = Field(default="", description="适用范围。")
    risk_level: str = Field(default="medium", description="风险等级：low/medium/high。")


class ScenarioPackResponse(BaseModel):
    """场景包/能力域（AGV-026/027）：业务目标+适用范围+风险等级，关联 Agent/eval/资产。"""

    scenario_pack_id: str
    name: str
    business_goal: str = ""
    scope: str = ""
    risk_level: str = "medium"
    created_at: str
    agent_ids: list[str] = Field(default_factory=list, description="装配了该场景包能力的 Agent。")
    eval_case_ids: list[str] = Field(default_factory=list)
    asset_refs: list[str] = Field(default_factory=list, description="关联的 prompt/skill/SOP/发布准入规则等资产引用。")
    merged_into: Optional[str] = Field(default=None, description="若被合并，指向主资产场景包 id（引用经此重定向，不丢失）。")


class DuplicateScenarioPackGroupResponse(BaseModel):
    """一组规范化重名的疑似重复场景包及合并建议（AGV-023）。"""

    normalized_name: str
    scenario_pack_ids: list[str]
    suggested_primary_id: str


class ScenarioPackMergeRequest(BaseModel):
    duplicate_ids: list[str] = Field(description="并入主资产的重复场景包 id 列表。")


class ScenarioPackAssociateRequest(BaseModel):
    agent_ids: Optional[list[str]] = Field(default=None, description="装配该场景包的 Agent（追加）。")
    eval_case_ids: Optional[list[str]] = Field(default=None, description="关联的 eval case（追加）。")
    asset_refs: Optional[list[str]] = Field(default=None, description="关联的资产引用（追加）。")


class ScenarioPackCopyRequest(BaseModel):
    name: str = Field(description="复制出的新场景包名称。")


class ScenarioPackAgentValidation(BaseModel):
    """跨 Agent 复用的单 Agent 验证结果（评估报告）。"""

    agent_id: str
    eval_runs: int = Field(default=0, description="该 Agent 已完成的评估运行数。")
    passed_eval_runs: int = Field(default=0, description="其中通过（passed/passed_with_notes）的评估运行数。")
    latest_result_status: Optional[str] = Field(default=None, description="最近一次完成评估的结果状态。")


class ScenarioPackReuseProvenanceResponse(BaseModel):
    """场景包跨 Agent 复用记录（AGV-010/045）：来源、适用范围、风险、方法论资产与跨 Agent 评估报告。"""

    scenario_pack_id: str
    source_pack_id: Optional[str] = Field(default=None, description="复用来源场景包（copied_from）；原创为 null。")
    risk_level: str = Field(description="复用风险等级。")
    scope_agent_ids: list[str] = Field(default_factory=list, description="复用范围：装配该场景包的业务 Agent。")
    methodology_asset_refs: list[str] = Field(default_factory=list, description="可复用方法论资产引用（prompt/skill/SOP/发布策略）。")
    methodology_eval_case_ids: list[str] = Field(default_factory=list, description="可复用评估用例（方法论资产）。")
    validation: list[ScenarioPackAgentValidation] = Field(
        default_factory=list, description="跨 Agent 评估报告：每个复用 Agent 保留独立评估结果。"
    )
