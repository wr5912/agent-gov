from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .json_types import JsonObject
from .runtime_db_base import Base, utc_now


class AgentAdmissionStateModel(Base):
    __tablename__ = "agent_admission_states"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    maintenance_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    maintenance_generation: Mapped[int] = mapped_column(Integer, default=0)
    maintenance_kind: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    maintenance_owner_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    maintenance_expires_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


class AgentWorktreeCleanupTaskModel(Base):
    __tablename__ = "agent_worktree_cleanup_tasks"

    change_set_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_change_sets.change_set_id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    delete_branch: Mapped[bool] = mapped_column(Boolean, default=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    claim_generation: Mapped[int] = mapped_column(Integer, default=0)
    claim_expires_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    next_retry_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    last_error_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AgentReleaseOperationModel(Base):
    __tablename__ = "agent_release_operations"

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    release_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_releases.release_id"),
        index=True,
    )
    operation_kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    expected_head_sha: Mapped[str] = mapped_column(String(64))
    target_commit_sha: Mapped[str] = mapped_column(String(64))
    release_expected_status: Mapped[str] = mapped_column(String(64))
    release_expected_updated_at: Mapped[str] = mapped_column(String(64))
    claim_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    claim_generation: Mapped[int] = mapped_column(Integer, default=0)
    claim_expires_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    operator: Mapped[str] = mapped_column(String(128))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    previous_head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    observed_head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    result_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    error_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


Index(
    "ux_agent_release_operations_identity",
    AgentReleaseOperationModel.operation_kind,
    AgentReleaseOperationModel.release_id,
    AgentReleaseOperationModel.expected_head_sha,
    unique=True,
)
