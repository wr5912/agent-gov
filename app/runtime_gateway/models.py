from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Index, Integer, LargeBinary, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import Base, utc_now


class RuntimeAgentVersionModel(Base):
    """一个不可变 AgentGov 版本到 AgentScope Agent 的绑定。"""

    __tablename__ = "runtime_agent_versions"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_version_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    harness_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    runtime_agent_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    governance_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    source_kind: Mapped[str] = mapped_column(String(32), default="published", index=True)
    source_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


class RuntimeAgentDeletionIntentModel(Base):
    """业务 Agent 一次删除代际的耐久远端清理账本。"""

    __tablename__ = "runtime_agent_deletion_intents"

    intent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_generation: Mapped[str] = mapped_column(String(64), index=True)
    workspace_dir: Mapped[str] = mapped_column(String(2048))
    versions_json: Mapped[list[JsonObject]] = mapped_column(JSON, default=list)
    sessions_json: Mapped[list[JsonObject]] = mapped_column(JSON, default=list)
    enumerated_runtime_agent_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    deleted_session_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    deleted_runtime_agent_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    removed_snapshot_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    tombstoned: Mapped[bool] = mapped_column(default=False)
    workspace_removed: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), default="cleanup_pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    error_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


Index(
    "ux_runtime_agent_deletion_one_pending",
    RuntimeAgentDeletionIntentModel.agent_id,
    unique=True,
    sqlite_where=text("status = 'cleanup_pending'"),
)


class RuntimeEphemeralResourceModel(Base):
    """候选/治理临时 AgentScope 资源的崩溃恢复定位账本。"""

    __tablename__ = "runtime_ephemeral_resources"

    cache_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    business_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    version_owner_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_version_id: Mapped[str] = mapped_column(String(256), index=True)
    harness_digest: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    runtime_agent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    workspace_id: Mapped[str] = mapped_column(String(384))
    status: Mapped[str] = mapped_column(String(32), default="provisioning", index=True)
    error_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class RuntimeSessionBindingModel(Base):
    """只保存 AgentScope Session 与治理版本的绑定，不保存消息。"""

    __tablename__ = "runtime_session_bindings"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_version_id: Mapped[str] = mapped_column(String(256), index=True)
    runtime_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    harness_digest: Mapped[str] = mapped_column(String(64))
    # Team worker Session 保留其顶层 leader Session 与 team 归属，使后续
    # AgentGov run 可以在不复制 AgentScope Team roster 的前提下重新绑定。
    root_session_id: Mapped[str] = mapped_column(String(128), index=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    active_run_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    active_team_generation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


class RuntimeSessionCreationIntentModel(Base):
    """先于上游创建持久化的 Session intent 与补偿审计记录。"""

    __tablename__ = "runtime_session_creation_intents"

    intent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(256),
        nullable=True,
        unique=True,
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_version_id: Mapped[str] = mapped_column(String(256), index=True)
    runtime_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    harness_digest: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[str] = mapped_column(String(320), index=True)
    session_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    error_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    cleanup_attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


Index(
    "ix_runtime_session_intents_recovery",
    RuntimeSessionCreationIntentModel.status,
    RuntimeSessionCreationIntentModel.updated_at,
)


class AgentRunModel(Base):
    """AgentGov 顶层 run；reply 与 Trace 都是关联标识而非别名。"""

    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_version_id: Mapped[str] = mapped_column(String(256), index=True)
    runtime_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    harness_digest: Mapped[str] = mapped_column(String(64))
    client_operation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    input_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    trigger_response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trigger_response_body: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    trigger_response_content_type: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    # reply_ids_json 是 Runtime lifecycle 已观察到的 expected 集合；下面
    # 两个集合分别记录 canonical Message 可读以及 Session state 已提交。
    reply_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    persisted_reply_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    persistence_batch_reply_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Team 消息必须先增加 generation 再进入 AgentScope inbox。只有 root 已
    # 持久化到最新 generation 且所有 child 均已完成相应批次时才可终态。
    team_generation: Mapped[int] = mapped_column(Integer, default=0)
    root_persisted_team_generation: Mapped[int] = mapped_column(Integer, default=0)
    pending_child_session_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    trace_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    trace_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    terminal_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    alert_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    metadata_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    started_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


Index(
    "ux_agent_runs_one_active_per_session",
    AgentRunModel.session_id,
    unique=True,
    sqlite_where=text("status IN ('queued','running','waiting_human','waiting_external','finalizing')"),
)

Index(
    "ux_agent_runs_client_operation",
    AgentRunModel.client_operation_id,
    unique=True,
    sqlite_where=text("client_operation_id IS NOT NULL"),
)


class RuntimePendingActionModel(Base):
    """AgentScope 原生 HITL 请求的最小、可校验投影。"""

    __tablename__ = "runtime_pending_actions"

    action_id: Mapped[str] = mapped_column(String(384), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    reply_id: Mapped[str] = mapped_column(String(128), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    tool_call_name: Mapped[str] = mapped_column(String(256))
    tool_call_json: Mapped[JsonObject] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    run_rules_json: Mapped[list[JsonObject]] = mapped_column(JSON, default=list)
    run_rules_granted_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    run_rules_expired_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now)


Index(
    "ux_runtime_pending_tool_call",
    RuntimePendingActionModel.session_id,
    RuntimePendingActionModel.reply_id,
    RuntimePendingActionModel.tool_call_id,
    unique=True,
)


class RuntimeReceiptModel(Base):
    """Runtime 生命周期回执去重账本。"""

    __tablename__ = "runtime_receipts"

    receipt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    reply_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    received_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


class RuntimeTeamDeliveryModel(Base):
    """跨 Session inbox 投递的幂等 quiescence 账本。"""

    __tablename__ = "runtime_team_deliveries"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    source_session_id: Mapped[str] = mapped_column(String(128), index=True)
    target_session_id: Mapped[str] = mapped_column(String(128), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


Index(
    "ux_runtime_team_delivery_generation",
    RuntimeTeamDeliveryModel.run_id,
    RuntimeTeamDeliveryModel.generation,
    unique=True,
)


class RuntimeCutoverLedgerModel(Base):
    """原子切换的可审计账本，不承载业务会话内容。"""

    __tablename__ = "runtime_cutover_ledger"

    cutover_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    phase: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    artifacts_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
