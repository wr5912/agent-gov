from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .runtime_db import Base, utc_now


class ImprovementItemModel(Base):
    """改进事项 ImprovementItem —— v2.7 跨代重建的事项级单一领域实体。

    它是 AgentGov 闭环治理的唯一事项级对象，归属到某个业务 Agent（agent_id），
    串起来源反馈、归因、方案、执行、回归与发布。阶段 improvement_stage 由集中状态机
    （state_machines."improvement_stage"）统一管理合法转移，improvement_status 为派生标签。
    表由 runtime_db.ensure_schema 的 create_all 创建（全新表，无需迁移改表）。
    """

    __tablename__ = "improvement_items"

    improvement_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(String(4096), default="")
    improvement_stage: Mapped[str] = mapped_column(String(64), default="feedback_intake", index=True)
    improvement_status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_feedback_refs_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
