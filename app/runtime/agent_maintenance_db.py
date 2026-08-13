from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, LargeBinary, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .json_types import JsonObject
from .runtime_db_base import Base, utc_now
from .state_machines import (
    WORKSPACE_ACTIVATION_ACTIONS,
    WORKSPACE_ACTIVATION_RECOVERY_PHASES,
    WORKSPACE_ACTIVATION_STATES,
)


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


class AgentWorkspaceActivationOperationModel(Base):
    __tablename__ = "agent_workspace_activation_operations"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({', '.join(repr(value) for value in sorted(WORKSPACE_ACTIVATION_STATES))})",
            name="ck_workspace_activation_state",
        ),
        CheckConstraint(
            f"action IN ({', '.join(repr(value) for value in sorted(WORKSPACE_ACTIVATION_ACTIONS))})",
            name="ck_workspace_activation_action",
        ),
        CheckConstraint(
            f"recovery_phase IN ({', '.join(repr(value) for value in sorted(WORKSPACE_ACTIVATION_RECOVERY_PHASES))})",
            name="ck_workspace_activation_recovery_phase",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    import_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    original_head_sha: Mapped[str] = mapped_column(String(64))
    base_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    candidate_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    candidate_tree_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    snapshot_created: Mapped[bool] = mapped_column(Boolean, default=False)
    original_status_text: Mapped[str] = mapped_column(Text, default="")
    original_index_fingerprint: Mapped[str] = mapped_column(String(64))
    original_workspace_fingerprint: Mapped[str] = mapped_column(String(64))
    original_index_tree_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    original_index_snapshot: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    recovery_phase: Mapped[str] = mapped_column(String(32), default="none")
    package_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tree_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    suite_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    suite_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    diagnostics_json: Mapped[list[JsonObject]] = mapped_column(JSON, default=list)
    maintenance_token: Mapped[str] = mapped_column(String(128), index=True)
    maintenance_generation: Mapped[int] = mapped_column(Integer)
    maintenance_expires_at: Mapped[str] = mapped_column(String(64), index=True)
    error_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


Index(
    "ux_agent_workspace_activation_operations_import",
    AgentWorkspaceActivationOperationModel.import_id,
    unique=True,
    sqlite_where=AgentWorkspaceActivationOperationModel.import_id.is_not(None),
)
Index(
    "ux_agent_workspace_activation_operations_fence",
    AgentWorkspaceActivationOperationModel.agent_id,
    unique=True,
    sqlite_where=text("state IN ('preparing', 'prepared', 'completing', 'rejecting', 'recovery_required')"),
)
