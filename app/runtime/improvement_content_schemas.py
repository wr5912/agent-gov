"""改进事项内容子资源 API 契约（v2.7 P3）：系统理解 + 归因。

字段所有权：请求 DTO 只承载用户可编辑的内容字段；id / status / 时间戳为 backend-owned，不入请求体。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NormalizedFeedbackUpsertRequest(BaseModel):
    problem: str = Field(description="问题（一句话）。")
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


class AttributionUpsertRequest(BaseModel):
    summary: str = Field(description="归因正文。")
    responsibility_boundary: list[str] = Field(default_factory=list, description="责任边界 bullets。")
    evidence: list[str] = Field(default_factory=list, description="证据要点。")


class ImprovementFeedbackCreateRequest(BaseModel):
    summary: str = Field(description="反馈摘要。")
    source: str = Field(default="playground_run", description="来源：playground_run/feedback_inbox/trace 等。")
    raw_text: str = Field(default="", description="反馈原文。")
    run_id: str = Field(default="", description="关联 Run。")
    session_id: str = Field(default="", description="关联 Session。")
    agent_version_id: str = Field(default="", description="反馈归属的 Agent 版本。")
    scenario: str = Field(default="", description="反馈归属的业务场景。")
    task_id: str = Field(default="", description="反馈归属的任务 ID。")
    alert_id: str = Field(default="", description="反馈归属的告警 ID。")
    case_id: str = Field(default="", description="反馈归属的 Case ID。")


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


class AttributionResponse(BaseModel):
    attribution_id: str
    improvement_id: str
    summary: str
    responsibility_boundary: list[str]
    evidence: list[str]
    status: str
    generated_by: str = "heuristic"
    created_at: str
    updated_at: str


class OptimizationChange(BaseModel):
    target: str = Field(description="变更对象（prompt/skill/profile/config 等）。")
    change: str = Field(description="变更描述。")


class OptimizationPlanUpsertRequest(BaseModel):
    summary: str = Field(description="方案正文。")
    changes: list[OptimizationChange] = Field(default_factory=list, description="变更项列表。")


class OptimizationPlanResponse(BaseModel):
    optimization_plan_id: str
    improvement_id: str
    summary: str
    changes: list[OptimizationChange]
    status: str
    generated_by: str = "heuristic"
    created_at: str
    updated_at: str


class ExecutionUpsertRequest(BaseModel):
    summary: str = Field(description="执行结果说明。")
    changes_applied: list[str] = Field(default_factory=list, description="已应用变更要点。")
    agent_version: str = Field(default="", description="生成的 Agent 版本标识。")


class ExecutionResponse(BaseModel):
    execution_id: str
    improvement_id: str
    summary: str
    changes_applied: list[str]
    agent_version: str
    status: str
    generated_by: str = "heuristic"
    change_set_id: str = ""
    applied_agent_version_id: str = ""
    applied_diff: dict = Field(default_factory=dict)
    created_at: str
    updated_at: str
