"""治理资产 Registry API 契约（v2.7 W3 复利中心）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AssetResponse(BaseModel):
    asset_id: str
    agent_id: str
    asset_type: str
    title: str
    body: str = ""
    source_improvement_id: str = ""
    inherited_from: str = Field(default="", description="非空表示该资产由此源资产继承而来（复利来源）。")
    created_at: str
    updated_at: str


class AssetCreateRequest(BaseModel):
    agent_id: str = Field(description="归属业务 Agent。")
    asset_type: str = Field(description="regression / methodology / execution / audit。")
    title: str = Field(description="资产标题。")
    body: str = Field(default="", description="资产正文（方法论/回归用例/执行脚本/审计说明）。")
    source_improvement_id: str = Field(default="", description="沉淀来源改进事项 ID（可空）。")


class AssetInheritRequest(BaseModel):
    target_agent_id: str = Field(description="把资产继承复用到的目标业务 Agent。")
