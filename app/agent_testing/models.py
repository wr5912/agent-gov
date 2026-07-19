from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import Base, utc_now


class AgentTestRunModel(Base):
    __tablename__ = "agent_test_runs"

    test_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    change_set_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    started_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    suite_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    command_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    suite_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    report_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    stdout_text: Mapped[str] = mapped_column(Text, default="")
    stderr_text: Mapped[str] = mapped_column(Text, default="")
    error_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_agent_test_runs_agent_created", AgentTestRunModel.agent_id, AgentTestRunModel.created_at)
Index("ix_agent_test_runs_change_commit", AgentTestRunModel.change_set_id, AgentTestRunModel.commit_sha)


class AgentTestRunItemModel(Base):
    __tablename__ = "agent_test_run_items"

    test_run_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    test_run_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_test_runs.test_run_id", ondelete="CASCADE"),
        index=True,
    )
    nodeid: Mapped[str] = mapped_column(String(2048))
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    phase: Mapped[str] = mapped_column(String(32), default="call")
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


Index("ux_agent_test_run_items_nodeid", AgentTestRunItemModel.test_run_id, AgentTestRunItemModel.nodeid, unique=True)


class AgentWorkspaceImportRecordModel(Base):
    __tablename__ = "agent_workspace_import_records"

    import_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    package_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tree_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    suite_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    warnings_json: Mapped[list[JsonObject]] = mapped_column(JSON, default=list)
    error_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
