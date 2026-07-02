from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from sqlalchemy.orm import sessionmaker

from app.runtime.claude_user_input_db import ClaudeUserInputRequestModel
from app.runtime.json_types import JsonObject
from app.runtime.records.claude_user_input_records import (
    ClaudeUserInputRequestRecord,
    ensure_transition,
    terminal_status_for_decision,
)
from app.runtime.runtime_db import utc_now


class ClaudeUserInputStore:
    """SQLite-backed audit store for Claude SDK user-input waits."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        request_id: str,
        decision_token_hash: str,
        business_agent_id: str,
        run_id: str,
        api_session_id: str,
        request_type: str,
        tool_name: str,
        input_json: JsonObject,
        context_json: JsonObject,
        risk_json: JsonObject,
        expires_at: str,
        sdk_session_id: Optional[str] = None,
        tool_use_id: Optional[str] = None,
        sdk_subagent_id: Optional[str] = None,
    ) -> ClaudeUserInputRequestRecord:
        now = utc_now()
        with self._session_factory.begin() as db:
            row = ClaudeUserInputRequestModel(
                request_id=request_id,
                decision_token_hash=decision_token_hash,
                business_agent_id=business_agent_id,
                run_id=run_id,
                api_session_id=api_session_id,
                sdk_session_id=sdk_session_id,
                tool_use_id=tool_use_id,
                sdk_subagent_id=sdk_subagent_id,
                request_type=request_type,
                tool_name=tool_name,
                input_json=input_json,
                context_json=context_json,
                risk_json=risk_json,
                status="waiting",
                decision=None,
                decision_payload_json={},
                decided_by=None,
                created_at=now,
                expires_at=expires_at,
                resolved_at=None,
            )
            db.add(row)
            db.flush()
            return self._record(row)

    def get(self, request_id: str) -> ClaudeUserInputRequestRecord | None:
        with self._session_factory() as db:
            row = db.get(ClaudeUserInputRequestModel, request_id)
            return self._record(row) if row else None

    def list(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        business_agent_id: str | None = None,
        limit: int = 100,
    ) -> list[ClaudeUserInputRequestRecord]:
        with self._session_factory() as db:
            query = db.query(ClaudeUserInputRequestModel)
            if session_id:
                query = query.filter(ClaudeUserInputRequestModel.api_session_id == session_id)
            if run_id:
                query = query.filter(ClaudeUserInputRequestModel.run_id == run_id)
            if status:
                query = query.filter(ClaudeUserInputRequestModel.status == status)
            if business_agent_id:
                query = query.filter(ClaudeUserInputRequestModel.business_agent_id == business_agent_id)
            rows = query.order_by(ClaudeUserInputRequestModel.created_at.desc()).limit(max(1, min(limit, 500))).all()
            return [self._record(row) for row in rows]

    def finish(
        self,
        request_id: str,
        *,
        decision: str,
        decision_payload_json: JsonObject,
        decided_by: str,
    ) -> ClaudeUserInputRequestRecord:
        next_status = terminal_status_for_decision(decision)
        with self._session_factory.begin() as db:
            row = db.get(ClaudeUserInputRequestModel, request_id)
            if row is None:
                raise KeyError(request_id)
            ensure_transition(row.status, next_status)
            row.status = next_status
            row.decision = decision
            row.decision_payload_json = decision_payload_json
            row.decided_by = decided_by
            row.resolved_at = utc_now()
            db.flush()
            return self._record(row)

    def cancel_waiting_requests(
        self,
        *,
        decision: str,
        decided_by: str,
        only_request_ids: Iterable[str] | None = None,
    ) -> list[ClaudeUserInputRequestRecord]:
        next_status = terminal_status_for_decision(decision)
        if next_status != "cancelled":
            raise ValueError("cancel_waiting_requests requires a cancellation decision")
        request_ids = set(only_request_ids or [])
        with self._session_factory.begin() as db:
            query = db.query(ClaudeUserInputRequestModel).filter(ClaudeUserInputRequestModel.status == "waiting")
            if request_ids:
                query = query.filter(ClaudeUserInputRequestModel.request_id.in_(request_ids))
            rows = query.all()
            records: list[ClaudeUserInputRequestRecord] = []
            now = utc_now()
            for row in rows:
                row.status = "cancelled"
                row.decision = decision
                row.decision_payload_json = {"message": decision}
                row.decided_by = decided_by
                row.resolved_at = now
                records.append(self._record(row))
            return records

    @staticmethod
    def _record(row: ClaudeUserInputRequestModel) -> ClaudeUserInputRequestRecord:
        return ClaudeUserInputRequestRecord(
            request_id=row.request_id,
            decision_token_hash=row.decision_token_hash,
            business_agent_id=row.business_agent_id,
            run_id=row.run_id,
            api_session_id=row.api_session_id,
            sdk_session_id=row.sdk_session_id,
            tool_use_id=row.tool_use_id,
            sdk_subagent_id=row.sdk_subagent_id,
            request_type=row.request_type,  # type: ignore[arg-type]
            tool_name=row.tool_name,
            input_json=dict(row.input_json or {}),
            context_json=dict(row.context_json or {}),
            risk_json=dict(row.risk_json or {}),
            status=row.status,  # type: ignore[arg-type]
            decision=row.decision,  # type: ignore[arg-type]
            decision_payload_json=dict(row.decision_payload_json or {}),
            decided_by=row.decided_by,
            created_at=row.created_at,
            expires_at=row.expires_at,
            resolved_at=row.resolved_at,
        )
