from __future__ import annotations

from sqlalchemy import Boolean, String
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
    # #26：来源——seed（声明式基线，禁删）vs user（用户创建，可 tombstone 删除）。建列由迁移 0018 保证。
    origin: Mapped[str] = mapped_column(String(16), default="user", index=True)
    # #26：删除 tombstone（用户删除时间）；非空表示已删除——discover/sync 跳过、list/get 过滤，重启不复活。
    deleted_at: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # 部署契约：agent.yaml requires_web_hitl 的投影（供运维/前端可见）。建列由迁移 0027 保证，
    # 启动 sync 按 profile.requires_web_hitl 校正。HITL 关时该 Agent 执行能力不可用。
    requires_web_hitl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
