from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .json_types import JsonObject
from .runtime_db_base import Base, utc_now


class ClaudeUserInputRequestModel(Base):
    """AgentGov audit projection for Claude SDK user-input waits."""

    __tablename__ = "claude_user_input_requests"
    __table_args__ = (
        Index("ix_claude_user_input_agent_status", "business_agent_id", "status", "created_at"),
        Index("ix_claude_user_input_run_status", "run_id", "status", "created_at"),
    )

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    decision_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    business_agent_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    api_session_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    sdk_session_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    tool_use_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    sdk_subagent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    request_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    input_json: Mapped[JsonObject] = mapped_column("redacted_input_json", JSON, default=dict)
    context_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    risk_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    decision: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    decision_payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    decided_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    expires_at: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    resolved_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
