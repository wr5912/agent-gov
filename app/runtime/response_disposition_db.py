from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .runtime_db_base import Base, utc_now


class ResponseDispositionClaimModel(Base):
    __tablename__ = "response_disposition_claims"
    __table_args__ = (
        Index("ix_response_disposition_claim_status", "status", "created_at"),
        Index("ix_response_disposition_claim_case", "case_id", "created_at"),
    )

    approval_request_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(256), nullable=False)
    playbook_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_run_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    response_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    create_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, nullable=False)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
