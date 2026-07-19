from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .runtime_db import Base, utc_now


class AgentRegistryModel(Base):
    """业务 Agent 身份注册表（AGV-004/022 基座）。

    被治理的业务 Agent 的稳定身份记录，作为运行、反馈、评估和版本治理的归属对象。
    治理 Agent（闭环执行者）不入注册表。业务 Agent 由运行态 Workspace 发现或 Workspace
    包导入创建，按 agent_id 隔离。建表由迁移 0007 保证（见 runtime_db_migrations）。
    """

    __tablename__ = "agent_registry"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32), index=True)
    workspace_dir: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    # 生命周期状态（AGV-020）：draft/active/evaluating/deprecated/archived。建列由迁移 0009 保证。
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    # #26：删除 tombstone（用户删除时间）；非空表示已删除——discover/sync 跳过、list/get 过滤，重启不复活。
    deleted_at: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # DB + workspace 创建 saga 的内部状态；provisioning 行不得进入公开查询或运行路径。
    provision_state: Mapped[str] = mapped_column(String(32), default="ready", nullable=False, index=True)
    provision_token: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    provision_started_at: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    provision_previous_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True, default=None)
