from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ._store_support import RuntimeStateConflict
from .contracts import RuntimeReceipt
from .hitl import HITLValidationError, parse_tool_call_fingerprint
from .models import AgentRunModel, RuntimePendingActionModel


def store_pending_actions(
    db: Session,
    *,
    run: AgentRunModel,
    receipt: RuntimeReceipt,
    kind: str,
    session_id: str | None = None,
) -> None:
    """持久化一次 HITL 请求的稳定 action/tool/Session 身份。"""

    reply_id = receipt.reply_id
    tool_calls = receipt.payload.get("tool_calls")
    if not reply_id or not isinstance(tool_calls, list) or not tool_calls:
        raise RuntimeStateConflict("HITL receipt is missing reply_id or tool_calls")
    mixed = db.scalar(
        select(RuntimePendingActionModel.action_id)
        .where(
            RuntimePendingActionModel.run_id == run.run_id,
            RuntimePendingActionModel.status == "pending",
            RuntimePendingActionModel.kind != kind,
        )
        .limit(1),
    )
    if mixed is not None:
        raise RuntimeStateConflict("Mixed human/external pending actions are not supported")
    for tool_call in tool_calls:
        try:
            fingerprint = parse_tool_call_fingerprint(tool_call)
        except HITLValidationError as exc:
            raise RuntimeStateConflict(str(exc)) from exc
        tool_id = fingerprint.tool_call_id
        tool_name = fingerprint.tool_call_name
        fingerprint_json = fingerprint.model_dump(mode="json")
        action_id = f"{run.run_id}:{reply_id}:{tool_id}"
        existing = db.get(RuntimePendingActionModel, action_id)
        if existing is not None:
            try:
                existing_fingerprint = parse_tool_call_fingerprint(
                    existing.tool_call_json,
                    expected_id=existing.tool_call_id,
                    expected_name=existing.tool_call_name,
                )
            except HITLValidationError as exc:
                raise RuntimeStateConflict(str(exc)) from exc
            if existing_fingerprint != fingerprint:
                raise RuntimeStateConflict(
                    "Pending tool call changed under the same identity",
                )
            continue
        db.add(
            RuntimePendingActionModel(
                action_id=action_id,
                session_id=session_id or run.session_id,
                run_id=run.run_id,
                reply_id=reply_id,
                tool_call_id=tool_id,
                kind=kind,
                tool_call_name=tool_name,
                tool_call_json=fingerprint_json,
            ),
        )
