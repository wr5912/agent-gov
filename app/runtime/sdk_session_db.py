from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .json_types import JsonObject
from .runtime_db_base import Base, utc_now


class SessionTurnIntentModel(Base):
    __tablename__ = "session_turn_intents"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    source_sdk_session_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    attempted_sdk_session_id: Mapped[str] = mapped_column(String(256), index=True)
    sdk_project_key: Mapped[str] = mapped_column(String(256), index=True)
    base_turns: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    request_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    error_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


Index(
    "ux_session_turn_intents_one_running",
    SessionTurnIntentModel.session_id,
    unique=True,
    sqlite_where=text("status = 'running'"),
)


class SdkSessionEntryModel(Base):
    __tablename__ = "sdk_session_entries"
    __table_args__ = (
        CheckConstraint(
            "NOT (committed_at IS NOT NULL AND discarded_at IS NOT NULL)",
            name="ck_sdk_session_entries_single_terminal_state",
        ),
    )

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_key: Mapped[str] = mapped_column(String(256), index=True)
    sdk_session_id: Mapped[str] = mapped_column(String(256), index=True)
    subpath: Mapped[str] = mapped_column(String(1024), default="")
    entry_uuid: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    entry_json: Mapped[JsonObject] = mapped_column(JSON)
    origin_run_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    committed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    discarded_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)


Index(
    "ux_sdk_session_entries_live_uuid",
    SdkSessionEntryModel.project_key,
    SdkSessionEntryModel.sdk_session_id,
    SdkSessionEntryModel.subpath,
    SdkSessionEntryModel.entry_uuid,
    unique=True,
    sqlite_where=text("entry_uuid IS NOT NULL AND discarded_at IS NULL"),
)
