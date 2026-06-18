from __future__ import annotations

from sqlalchemy import JSON, String, Text
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


class ImprovementLinkModel(Base):
    """改进事项 ↔ 既有闭环对象的轻引用（v2.7 W2-c）。

    kind 标明被引对象类型（attribution / optimization_plan / eval_run / change_set / batch），
    ref_id 为该对象 ID。独立新表，create_all 创建，无需改表迁移；不在 improvement_items 上加列。
    """

    __tablename__ = "improvement_links"

    link_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    improvement_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    ref_id: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


class NormalizedFeedbackModel(Base):
    """系统理解 NormalizedFeedback（v2.7 §4/§6 P3）：把自然语言反馈整理成可确认的结构化理解。

    与改进事项 1:1（improvement_id 唯一）。status：draft（系统初步整理）/ confirmed（用户已确认）。
    独立新表，create_all 创建，无需改表迁移。
    """

    __tablename__ = "normalized_feedbacks"

    normalized_feedback_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    improvement_id: Mapped[str] = mapped_column(String(128), index=True, unique=True)
    problem: Mapped[str] = mapped_column(String(1024), default="")
    possible_reason: Mapped[str] = mapped_column(String(1024), default="")
    possible_object: Mapped[str] = mapped_column(String(512), default="")
    impact: Mapped[str] = mapped_column(String(128), default="")
    suggestion: Mapped[str] = mapped_column(String(1024), default="")
    user_quote: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now)


class ImprovementFeedbackModel(Base):
    """改进事项来源反馈 Feedback（v2.7 §8.4 P3）：一等反馈内容（摘要/来源/状态/原文/Run-Trace）。

    与改进事项 1:多（improvement_id index）。source：playground_run/feedback_inbox/trace 等；
    status：merged/standalone 等。区别于 pre-v2.7 的 feedback_signals(旧反馈优化 workspace)。独立新表。
    """

    __tablename__ = "improvement_feedbacks"

    feedback_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    improvement_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), default="main-agent")
    summary: Mapped[str] = mapped_column(String(1024), default="")
    source: Mapped[str] = mapped_column(String(64), default="playground_run")
    status: Mapped[str] = mapped_column(String(32), default="merged")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[str] = mapped_column(String(128), default="")
    session_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


class AttributionModel(Base):
    """归因结果 Attribution（v2.7 §6 P3）：归因正文 + 责任边界 + 证据 + 确认状态。

    与改进事项 1:1（improvement_id 唯一）。status：draft / confirmed。独立新表，create_all 创建。
    """

    __tablename__ = "attributions"

    attribution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    improvement_id: Mapped[str] = mapped_column(String(128), index=True, unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    responsibility_boundary_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now)


class OptimizationPlanModel(Base):
    """优化方案 OptimizationPlan（v2.7 §6→optimization P3，草图 §106）：方案正文 + 变更项 + 确认状态。

    与改进事项 1:1。变更项 changes_json：[{target, change}]。status：draft / confirmed。独立新表。
    """

    __tablename__ = "optimization_plans"

    optimization_plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    improvement_id: Mapped[str] = mapped_column(String(128), index=True, unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    changes_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now)


class ExecutionRecordModel(Base):
    """执行记录 ExecutionRecord（v2.7 execution P3，草图 §107）：执行结果 + 已应用变更 + Agent 版本 + 状态。

    与改进事项 1:1。status：draft / confirmed（确认=已应用/已生成版本）。独立新表。
    """

    __tablename__ = "execution_records"

    execution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    improvement_id: Mapped[str] = mapped_column(String(128), index=True, unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    changes_applied_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    agent_version: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now)
