from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .runtime_db import Base, utc_now


class AssetModel(Base):
    """治理资产（四阶段改进治理 W3 资产 Registry 复利中心）。

    资产是从改进闭环沉淀下来、可跨业务 Agent 复用的方法论/回归/执行/审计资产。
    inherited_from 记录被继承的源资产 ID（复利来源），source_improvement_id 记录沉淀来源事项。
    独立新表，create_all 创建，无需改表迁移。
    """

    __tablename__ = "governance_assets"

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    asset_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text, default="")
    source_improvement_id: Mapped[str] = mapped_column(String(128), default="")
    inherited_from: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
