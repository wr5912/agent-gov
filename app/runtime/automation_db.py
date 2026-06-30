from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .runtime_db import Base, utc_now


class AutomationPolicyModel(Base):
    """自动化策略（四阶段改进治理 W2）：按业务 Agent 配置改进事项的自动推进模式。

    mode: off（全人工触发，默认）/ semi（自动推进至关键判断点停下）/ full（自动推进至发布门禁前）。
    表由 runtime_db.ensure_schema 的 create_all 创建（全新表，无需改表迁移）。
    """

    __tablename__ = "automation_policies"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="off")
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now)
