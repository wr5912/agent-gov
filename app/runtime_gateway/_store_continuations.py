from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ._store_support import RuntimeInputRejected, RuntimeStateConflict
from .contracts import ConfirmationScope, RunStatus, confirmation_reply_id
from .hitl import HITLValidationError, validate_pending_actions
from .models import AgentRunModel, RuntimePendingActionModel, RuntimeSessionBindingModel


def require_continuation_run(
    db: Session,
    *,
    binding: RuntimeSessionBindingModel,
    client_operation_id: str | None,
    expected_run_id: str | None,
) -> tuple[AgentRunModel, str]:
    if not client_operation_id:
        raise RuntimeInputRejected(
            "client_operation_id is required for a HITL continuation",
        )
    if not expected_run_id:
        raise RuntimeStateConflict(
            "Decision expected_run_id does not match the active run",
        )
    run = db.get(AgentRunModel, expected_run_id)
    if run is None:
        raise RuntimeStateConflict(
            "Decision expected_run_id does not match the active run",
        )
    if run.session_id != binding.root_session_id or run.session_id != binding.session_id or run.runtime_agent_id != binding.runtime_agent_id:
        raise RuntimeStateConflict("Decision does not match the governed root Session")
    return run, client_operation_id


def validate_new_continuation(
    db: Session,
    *,
    binding: RuntimeSessionBindingModel,
    run: AgentRunModel,
    input_value: Any,
    confirmation_scope: ConfirmationScope,
) -> None:
    if binding.active_run_id != run.run_id or RunStatus(run.status) not in {RunStatus.WAITING_HUMAN, RunStatus.WAITING_EXTERNAL}:
        raise RuntimeStateConflict("Session is not waiting for an external decision")
    if (run.metadata_json or {}).get("recovery_required") is True:
        raise RuntimeStateConflict(
            "Run recovery must complete before any HITL continuation",
        )
    if not isinstance(input_value, dict):  # pragma: no cover - public model narrows
        raise RuntimeStateConflict("HITL continuation must be an object")
    event_type = input_value.get("type")
    expected_status = RunStatus.WAITING_HUMAN if event_type == "USER_CONFIRM_RESULT" else RunStatus.WAITING_EXTERNAL
    if RunStatus(run.status) != expected_status:
        raise RuntimeStateConflict("Decision type does not match the pending action kind")
    reply_id = confirmation_reply_id(input_value)
    expected_kind = "human" if event_type == "USER_CONFIRM_RESULT" else "external"
    pending = db.scalar(
        select(RuntimePendingActionModel.action_id)
        .where(
            RuntimePendingActionModel.run_id == run.run_id,
            RuntimePendingActionModel.reply_id == reply_id,
            RuntimePendingActionModel.kind == expected_kind,
            RuntimePendingActionModel.status == "pending",
        )
        .limit(1),
    )
    if not reply_id or pending is None:
        raise RuntimeStateConflict("Decision does not match the active reply")
    try:
        validate_pending_actions(
            db,
            run=run,
            reply_id=reply_id,
            input_value=input_value,
            confirmation_scope=confirmation_scope,
        )
    except HITLValidationError as exc:
        raise RuntimeStateConflict(str(exc)) from exc
