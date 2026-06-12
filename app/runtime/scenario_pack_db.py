from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .json_types import JsonObject
from .runtime_db import Base, utc_now


class ScenarioPackModel(Base):
    """场景包/能力域（AGV-026/027）：把治理资产按业务场景组织的一等对象。

    场景包表达业务目标、适用范围和风险等级，并关联 Agent、eval case、资产引用，
    可被 Agent 装配能力、可跨 Agent 复用。关联关系存于 payload_json（agent_ids、
    eval_case_ids、asset_refs），避免为可变关联反复加列。建表由迁移 0010 保证。
    """

    __tablename__ = "scenario_packs"

    scenario_pack_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    business_goal: Mapped[str] = mapped_column(String(2048), default="")
    scope: Mapped[str] = mapped_column(String(2048), default="")
    risk_level: Mapped[str] = mapped_column(String(32), default="medium", index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
