from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .runtime_db import Base, utc_now


class AgentRegistryModel(Base):
    """业务 Agent 身份注册表（AGV-004/022 基座）。

    被治理的业务 Agent 的稳定身份记录，作为运行、反馈、评估和版本治理的归属对象。
    治理 Agent（闭环执行者）不入注册表。当前以 main-agent 为首条种子，向多业务
    Agent 扩展时按 agent_id 追加。建表由迁移 0007 保证（见 runtime_db_migrations）。
    """

    __tablename__ = "agent_registry"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32), index=True)
    workspace_dir: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    # 生命周期状态（AGV-020）：draft/active/evaluating/deprecated/archived。建列由迁移 0009 保证。
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
