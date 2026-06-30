"""自动化策略与自动推进 API 契约（四阶段改进治理 W2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.runtime.improvement_schemas import ImprovementItemResponse


class AutomationPolicyResponse(BaseModel):
    agent_id: str
    mode: str = Field(description="off / semi / full。")


class AutomationPolicyUpdateRequest(BaseModel):
    agent_id: str
    mode: str = Field(description="off / semi / full；非法值 400。")


class AutoAdvanceResponse(BaseModel):
    improvement: ImprovementItemResponse
    applied_stages: list[str] = Field(default_factory=list, description="本次自动推进经过的阶段序列。")
    stopped_reason: str = Field(description="policy_off / archived / gate_confirmation / release_gate / terminal。")
