from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from app.runtime.response_disposition_db import ResponseDispositionClaimModel

from .base import StrictRuntimeRecord

ResponseDispositionClaimStatus = Literal["claimed", "completed", "failed", "cancelled"]


class ResponseDispositionClaimRecord(StrictRuntimeRecord):
    approval_request_id: str
    case_id: str
    playbook_digest: str
    execution_run_id: str
    status: ResponseDispositionClaimStatus
    response_id: str | None = None
    agent_run_id: str | None = None
    create_authorized: bool = False
    manual_authorized: bool = False
    failure_reason: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ResponseDispositionClaimRecord:
        for field_name in ("approval_request_id", "case_id", "playbook_digest", "execution_run_id", "created_at", "updated_at"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} cannot be empty")
        return self

    @classmethod
    def from_row(cls, row: ResponseDispositionClaimModel) -> ResponseDispositionClaimRecord:
        return cls.model_validate(
            {
                "approval_request_id": row.approval_request_id,
                "case_id": row.case_id,
                "playbook_digest": row.playbook_digest,
                "execution_run_id": row.execution_run_id,
                "response_id": row.response_id,
                "agent_run_id": row.agent_run_id,
                "status": row.status,
                "create_authorized": row.create_authorized,
                "manual_authorized": row.manual_authorized,
                "failure_reason": row.failure_reason,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "completed_at": row.completed_at,
            }
        )
