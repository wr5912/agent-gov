from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.records.response_disposition_records import ResponseDispositionClaimRecord
from app.runtime.response_disposition_control import (
    SOC_CREATE_TOOL,
    SOC_MANUAL_TOOL,
    TrustedResponseDispositionContext,
)
from app.runtime.response_disposition_db import ResponseDispositionClaimModel
from app.runtime.runtime_db import utc_now
from app.runtime.state_machines import validate_transition


class ResponseDispositionClaimError(RuntimeError):
    pass


class ResponseDispositionClaimConflict(ResponseDispositionClaimError):
    pass


class ResponseDispositionClaimNotFound(ResponseDispositionClaimError):
    pass


class ResponseDispositionClaimStore:
    """Persist only AgentGov's one-shot consumption fact, never the RO playbook snapshot."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def claim(self, context: TrustedResponseDispositionContext) -> ResponseDispositionClaimRecord:
        if context.phase != "approved_execution":
            raise ValueError("Only approved_execution can create a disposition claim")
        if not context.approval_request_id or not context.playbook_digest or not context.execution_run_id:
            raise ValueError("approved_execution claim requires approval, digest, and execution bindings")
        now = utc_now()
        try:
            with self._session_factory.begin() as db:
                row = ResponseDispositionClaimModel(
                    approval_request_id=context.approval_request_id,
                    case_id=context.case_id,
                    playbook_digest=context.playbook_digest,
                    execution_run_id=context.execution_run_id,
                    status="claimed",
                    create_authorized=False,
                    manual_authorized=False,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                return ResponseDispositionClaimRecord.from_row(row)
        except IntegrityError as exc:
            raise ResponseDispositionClaimConflict("approval_request_id or execution_run_id has already been consumed") from exc

    def get(self, approval_request_id: str) -> ResponseDispositionClaimRecord | None:
        with self._session_factory() as db:
            row = db.get(ResponseDispositionClaimModel, approval_request_id)
            return ResponseDispositionClaimRecord.from_row(row) if row else None

    def bind_run(self, approval_request_id: str, *, agent_run_id: str) -> ResponseDispositionClaimRecord:
        with self._session_factory.begin() as db:
            row = self._required_row(db, approval_request_id)
            self._require_claimed(row)
            if row.agent_run_id and row.agent_run_id != agent_run_id:
                raise ResponseDispositionClaimConflict("claim is already bound to a different AgentGov run")
            row.agent_run_id = agent_run_id
            row.response_id = f"resp_{agent_run_id}"
            row.updated_at = utc_now()
            db.flush()
            return ResponseDispositionClaimRecord.from_row(row)

    def mark_tool_authorized(self, approval_request_id: str, tool_name: str) -> ResponseDispositionClaimRecord:
        with self._session_factory.begin() as db:
            row = self._required_row(db, approval_request_id)
            self._require_claimed(row)
            if tool_name == SOC_CREATE_TOOL:
                if row.create_authorized or row.manual_authorized:
                    raise ResponseDispositionClaimConflict("SOC create is duplicated or occurs after manual")
                row.create_authorized = True
            elif tool_name == SOC_MANUAL_TOOL:
                if row.manual_authorized:
                    raise ResponseDispositionClaimConflict("SOC manual is duplicated")
                row.manual_authorized = True
            else:
                raise ResponseDispositionClaimConflict(f"tool is not authorized by response disposition: {tool_name}")
            row.updated_at = utc_now()
            db.flush()
            return ResponseDispositionClaimRecord.from_row(row)

    def finish(self, approval_request_id: str, *, target: str, reason: str | None = None) -> ResponseDispositionClaimRecord:
        with self._session_factory.begin() as db:
            row = self._required_row(db, approval_request_id)
            validate_transition("response_disposition_claim", row.status, target)
            if target == "completed" and not row.manual_authorized:
                raise ResponseDispositionClaimConflict("claim cannot complete before SOC manual is authorized")
            now = utc_now()
            row.status = target
            row.failure_reason = reason
            row.updated_at = now
            row.completed_at = now
            db.flush()
            return ResponseDispositionClaimRecord.from_row(row)

    def cancel_orphan_claims(self, *, reason: str = "service_restarted") -> list[ResponseDispositionClaimRecord]:
        with self._session_factory.begin() as db:
            rows = db.query(ResponseDispositionClaimModel).filter(ResponseDispositionClaimModel.status == "claimed").all()
            now = utc_now()
            records: list[ResponseDispositionClaimRecord] = []
            for row in rows:
                validate_transition("response_disposition_claim", row.status, "cancelled")
                row.status = "cancelled"
                row.failure_reason = reason
                row.updated_at = now
                row.completed_at = now
                records.append(ResponseDispositionClaimRecord.from_row(row))
            return records

    @staticmethod
    def _required_row(db: Session, approval_request_id: str) -> ResponseDispositionClaimModel:
        row = db.get(ResponseDispositionClaimModel, approval_request_id)
        if row is None:
            raise ResponseDispositionClaimNotFound(f"response disposition claim not found: {approval_request_id}")
        return row

    @staticmethod
    def _require_claimed(row: ResponseDispositionClaimModel) -> None:
        if row.status != "claimed":
            raise ResponseDispositionClaimConflict(f"response disposition claim is already {row.status}")
