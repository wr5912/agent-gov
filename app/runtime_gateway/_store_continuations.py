from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ._store_operations import _continuation_request_shape, _submitted_tool_call_ids
from ._store_support import RuntimeStateConflict
from .contracts import ConfirmationScope, RunStatus, confirmation_reply_id
from .hitl import HITLValidationError, validate_pending_actions
from .models import AgentRunModel, RuntimePendingActionModel, RuntimeSessionBindingModel


def require_continuation_run(
    db: Session,
    *,
    binding: RuntimeSessionBindingModel,
    input_value: Any,
) -> AgentRunModel:
    """由原生 reply/tool 身份与受控 root/child Session 绑定唯一定位 Run。"""
    shape = _continuation_request_shape(input_value)
    tool_call_ids = _submitted_tool_call_ids(shape.submitted, human=shape.pending_kind == "human")
    rows = db.execute(
        select(RuntimePendingActionModel.run_id, RuntimePendingActionModel.session_id, RuntimePendingActionModel.tool_call_id, RuntimePendingActionModel.kind)
        .join(AgentRunModel, AgentRunModel.run_id == RuntimePendingActionModel.run_id)
        .where(
            RuntimePendingActionModel.reply_id == shape.reply_id,
            RuntimePendingActionModel.tool_call_id.in_(tool_call_ids),
            RuntimePendingActionModel.status == "pending",
            AgentRunModel.session_id == binding.root_session_id,
            AgentRunModel.agent_id == binding.agent_id,
        )
    ).all()
    if len(rows) != len(tool_call_ids) or {row.tool_call_id for row in rows} != set(tool_call_ids):
        raise RuntimeStateConflict("Decision references an unknown or ambiguous tool call")
    if any(row.kind != shape.pending_kind for row in rows):
        raise RuntimeStateConflict("Decision type does not match the pending action kind")
    run_ids = {row.run_id for row in rows}
    action_sessions = {row.session_id for row in rows}
    if len(run_ids) != 1 or len(action_sessions) != 1:
        raise RuntimeStateConflict("Decision references ambiguous Run or worker Session identities")
    run = db.get(AgentRunModel, run_ids.pop())
    action_session_id = action_sessions.pop()
    if run is None or run.session_id != binding.root_session_id or run.agent_id != binding.agent_id or binding.session_id != action_session_id:
        raise RuntimeStateConflict("Decision must be submitted to the exact pending action Session")
    return run


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
