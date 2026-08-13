from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Boolean, CheckConstraint, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .json_types import JsonObject
from .runtime_db_base import Base, utc_now
from .state_machines import AGENT_DELETION_STATES


class AgentDeletionOperationModel(Base):
    """Durable authority for one exact business-Agent instance deletion."""

    __tablename__ = "agent_deletion_operations"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({', '.join(repr(value) for value in sorted(AGENT_DELETION_STATES))})",
            name="ck_agent_deletion_operation_state",
        ),
        CheckConstraint(
            "NOT purge_confirmed OR quarantine_confirmed",
            name="ck_agent_deletion_purge_requires_quarantine",
        ),
        CheckConstraint(
            "state != 'completed' OR (quarantine_confirmed AND purge_confirmed)",
            name="ck_agent_deletion_completed_cleanup",
        ),
        CheckConstraint(
            "NOT witness_removed OR state = 'completed'",
            name="ck_agent_deletion_witness_terminal",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256))
    agent_id: Mapped[str] = mapped_column(String(128))
    agent_instance_etag: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32))
    workspace_path: Mapped[str] = mapped_column(String(2048))
    expected_device: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expected_inode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expected_mount_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quarantine_path: Mapped[str] = mapped_column(String(2048))
    quarantine_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    purge_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    witness_removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    deleted_json: Mapped[JsonObject] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    impact_json: Mapped[JsonObject] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    error_json: Mapped[JsonObject] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


Index(
    "ux_agent_deletion_operations_idempotency",
    AgentDeletionOperationModel.idempotency_key,
    unique=True,
)
Index(
    "ix_agent_deletion_operations_agent_instance",
    AgentDeletionOperationModel.agent_id,
    AgentDeletionOperationModel.agent_instance_etag,
)
Index(
    "ux_agent_deletion_operations_pending_agent",
    AgentDeletionOperationModel.agent_id,
    unique=True,
    sqlite_where=text("state = 'cleanup_pending'"),
)
Index(
    "ix_agent_deletion_operations_state_updated",
    AgentDeletionOperationModel.state,
    AgentDeletionOperationModel.updated_at,
    AgentDeletionOperationModel.created_at,
    AgentDeletionOperationModel.operation_id,
)
Index(
    "ix_agent_deletion_operations_witness_cleanup",
    AgentDeletionOperationModel.state,
    AgentDeletionOperationModel.witness_removed,
    AgentDeletionOperationModel.updated_at,
    AgentDeletionOperationModel.completed_at,
    AgentDeletionOperationModel.operation_id,
    sqlite_where=text("state = 'completed' AND witness_removed = 0"),
)
