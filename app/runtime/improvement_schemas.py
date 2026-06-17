"""改进事项 ImprovementItem 的 API 契约（v2.7 跨代重建，统一术语单一来源）。

字段所有权：请求 DTO 只承载用户/前端可提供的字段（agent_id、title、summary、source_feedback_refs、
目标 stage）；backend-owned 的 improvement_id / improvement_stage / improvement_status 不出现在请求体里，
由后端生成与状态机管理——这样 agent / 外部输入无法越权覆盖后端权威字段。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ImprovementCreateRequest(BaseModel):
    agent_id: str = Field(description="归属业务 Agent 的稳定 ID（治理归属主键）。")
    title: str = Field(description="改进事项标题。")
    summary: str = Field(default="", description="改进事项摘要/系统理解，可空。")
    source_feedback_refs: list[str] = Field(default_factory=list, description="来源反馈 ID 列表（轻引用）。")


class ImprovementStageTransitionRequest(BaseModel):
    stage: str = Field(
        description="目标阶段：feedback_intake/triage/attribution/optimization/execution/regression/release；"
        "非法转移由后端状态机拒绝（409）。",
    )


class ImprovementItemResponse(BaseModel):
    improvement_id: str
    agent_id: str
    title: str
    summary: str = ""
    source_feedback_refs: list[str] = Field(default_factory=list)
    improvement_stage: str = Field(description="事项阶段（后端状态机管理）。")
    improvement_status: str = Field(default="active", description="派生状态：active/done/archived。")
    created_at: str
    updated_at: str
