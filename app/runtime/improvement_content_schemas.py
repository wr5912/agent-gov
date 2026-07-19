"""改进事项内容子资源 API 契约（四阶段改进治理 P3）：系统理解 + 归因。

字段所有权：请求 DTO 只承载用户可编辑的内容字段；id / status / 时间戳为 backend-owned，不入请求体。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.agent_testing.schemas import AgentTestRunResponse

from .improvement_feedback_contract import FEEDBACK_CASE_ATTACH_ONLY_MESSAGE, has_feedback_case_semantics


class NormalizedFeedbackUpsertRequest(BaseModel):
    problem: str = Field(min_length=1, description="问题（一句话）。")
    possible_reason: str = Field(default="", description="可能原因。")
    possible_object: str = Field(default="", description="可能对象。")
    impact: str = Field(default="", description="影响（高/中/低或描述）。")
    suggestion: str = Field(default="", description="建议方向。")
    user_quote: str = Field(default="", description="用户原话。")


class NormalizedFeedbackResponse(BaseModel):
    normalized_feedback_id: str
    improvement_id: str
    problem: str
    possible_reason: str
    possible_object: str
    impact: str
    suggestion: str
    user_quote: str
    status: str
    created_at: str
    updated_at: str
    generated_by: str = "heuristic"
    generation_trace_id: str = ""
    generation_trace_url: str = ""


class AttributionUpsertRequest(BaseModel):
    summary: str = Field(min_length=1, description="归因正文。")
    responsibility_boundary: list[str] = Field(default_factory=list, description="责任边界 bullets。")
    evidence: list[str] = Field(default_factory=list, description="证据要点。")


class ImprovementFeedbackCreateRequest(BaseModel):
    summary: str = Field(min_length=1, description="反馈摘要。")
    source: str = Field(default="playground_run", description="通用反馈来源，例如 playground_run/trace；FeedbackCase 必须走专用挂接接口。")
    raw_text: str = Field(default="", description="反馈原文。")
    run_id: str = Field(default="", description="关联 Run。")
    session_id: str = Field(default="", description="关联 Session。")
    agent_version_id: str = Field(default="", description="反馈归属的 Agent 版本。")
    scenario: str = Field(default="", description="反馈归属的业务场景。")
    task_id: str = Field(default="", description="反馈归属的任务 ID。")
    alert_id: str = Field(default="", description="反馈归属的告警 ID。")
    case_id: str = Field(default="", description="反馈归属的业务 Case ID，不接受 FeedbackCase ID。")

    @model_validator(mode="after")
    def _reject_feedback_case_semantics(self) -> ImprovementFeedbackCreateRequest:
        if has_feedback_case_semantics(source=self.source, case_id=self.case_id):
            raise ValueError(FEEDBACK_CASE_ATTACH_ONLY_MESSAGE)
        return self


class ImprovementFeedbackResponse(BaseModel):
    feedback_id: str
    improvement_id: str
    agent_id: str
    summary: str
    source: str
    status: str
    raw_text: str
    run_id: str
    session_id: str
    agent_version_id: str
    scenario: str
    task_id: str
    alert_id: str
    case_id: str
    created_at: str


class ImprovementFeedbackReassignRequest(BaseModel):
    target_improvement_id: str = Field(description="把该反馈移动到的目标改进事项 ID（跨事项调整）。")


class AttachFeedbackCaseRequest(BaseModel):
    feedback_case_id: str = Field(description="要归入当前事项的已有反馈 Case（fbc-…）。")


class AttachableFeedbackCase(BaseModel):
    feedback_case_id: str
    title: str
    status: str
    run_ids: list[str] = Field(default_factory=list)


class AttachableFeedbacksResponse(BaseModel):
    feedback_cases: list[AttachableFeedbackCase] = Field(default_factory=list, description="未归属于任何改进事项的一等反馈 Case 池。")
    other_improvement_feedbacks: list[ImprovementFeedbackResponse] = Field(
        default_factory=list, description="其他改进事项中、同一业务 Agent 的反馈，可调整过来。"
    )


class ImprovementDeletionImpactResponse(BaseModel):
    improvement_id: str
    title: str
    source_feedback_refs: int
    feedbacks: int
    links: int
    has_attribution: bool
    has_optimization_plan: bool


class AttributionResponse(BaseModel):
    attribution_id: str
    improvement_id: str
    summary: str
    responsibility_boundary: list[str]
    evidence: list[str]
    counter_evidence: list[str] = Field(default_factory=list, description="反证：与归因相悖的观察（agent-owned）。")
    uncertainty_factors: list[str] = Field(default_factory=list, description="不确定性因素（agent-owned）。")
    verification_suggestions: list[str] = Field(default_factory=list, description="验证建议（agent-owned）。")
    status: str
    generated_by: str = "heuristic"
    generation_trace_id: str = ""
    generation_trace_url: str = ""
    created_at: str
    updated_at: str


class OptimizationChange(BaseModel):
    target: str = Field(min_length=1, description="变更对象（prompt/skill/profile/config 等）。")
    change: str = Field(min_length=1, description="变更描述。")


class OptimizationPlanUpsertRequest(BaseModel):
    summary: str = Field(min_length=1, description="方案正文。")
    changes: list[OptimizationChange] = Field(min_length=1, description="变更项列表。")


class OptimizationPlanResponse(BaseModel):
    optimization_plan_id: str
    improvement_id: str
    summary: str
    changes: list[OptimizationChange]
    risk_level: str = Field(default="", description="方案风险级别（agent-owned，来自 formatter risk）。")
    status: str
    generated_by: str = "heuristic"
    generation_trace_id: str = ""
    generation_trace_url: str = ""
    created_at: str
    updated_at: str


class RegressionGeneratedTest(BaseModel):
    target_path: str = Field(min_length=1, description="后端确定的 Workspace pytest 目标路径。")
    test_code: str = Field(min_length=1, description="治理 Agent 生成的完整 pytest 模块代码。")
    test_intent: str = Field(min_length=1, description="测试意图。")
    assertion_rationale: str = Field(min_length=1, description="断言为何能验证修复效果。")


class RegressionTestDesignResponse(BaseModel):
    regression_test_design_id: str
    improvement_id: str
    summary: str
    tests: list[RegressionGeneratedTest]
    no_action_reason: str = ""
    status: str
    generated_by: str = "heuristic"
    generation_trace_id: str = ""
    generation_trace_url: str = ""
    created_at: str
    updated_at: str
    generated_test_files: list[str] = Field(default_factory=list)
    candidate_commit_sha: str = ""
    test_run: AgentTestRunResponse | None = None


class ExecutionResponse(BaseModel):
    execution_id: str
    improvement_id: str
    summary: str
    changes_applied: list[str]
    agent_version: str
    risk_level: str = Field(default="", description="执行风险级别（agent-owned）。")
    rollback_strategy: str = Field(default="", description="回滚策略（agent-owned）。")
    rollback_instructions: list[str] = Field(default_factory=list, description="回滚步骤（agent-owned）。")
    status: str
    generated_by: str = "heuristic"
    change_set_id: str = ""
    applied_agent_version_id: str = ""
    applied_diff: dict = Field(default_factory=dict)
    generation_trace_id: str = ""
    generation_trace_url: str = ""
    created_at: str
    updated_at: str
