from __future__ import annotations

from sqlalchemy.orm import Session

from ._store_run_recovery import (
    _validated_interrupted_session_ids,
    validate_runtime_interrupted_receipt,
)
from ._store_support import RuntimeObjectNotFound, RuntimeStateConflict, _canonical_json, _require_run
from .contracts import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    RunStatus,
    RuntimeReceipt,
)
from .models import (
    AgentRunModel,
    RuntimeReceiptModel,
    RuntimeSessionBindingModel,
)


def validate_duplicate_receipt(
    recorded: RuntimeReceiptModel,
    incoming: RuntimeReceipt,
    run: AgentRunModel,
) -> None:
    """同一幂等身份只能重放完全相同的控制事实。"""

    unchanged = (
        recorded.receipt_id == incoming.receipt_id
        and recorded.event_id == incoming.event_id
        and recorded.run_id == incoming.run_id
        and recorded.session_id == incoming.session_id
        and recorded.reply_id == incoming.reply_id
        and recorded.event_type == incoming.type
        and incoming.trace_id == run.trace_id
        and (
            incoming.type != "RUN_INTERRUPTED"
            or (
                incoming.run_id == recorded.run_id
                and incoming.trace_id == run.trace_id
                and RunStatus(run.status)
                in {
                    *ACTIVE_RUN_STATUSES,
                    RunStatus.CANCELLED,
                    RunStatus.INTERRUPTED,
                }
            )
        )
        and _canonical_json(recorded.payload_json or {}) == _canonical_json(incoming.payload)
    )
    if not unchanged:
        raise RuntimeStateConflict("Receipt idempotency identity was rebound")


def resolve_receipt_run(
    db: Session,
    receipt: RuntimeReceipt,
) -> tuple[AgentRunModel, RuntimeSessionBindingModel | None, bool]:
    """解析精确 run/fence，并指出该 Session 的中断证据是否已持久化。"""

    binding = db.get(RuntimeSessionBindingModel, receipt.session_id)
    if receipt.type != "RUN_INTERRUPTED":
        if binding is None or not binding.active_run_id:
            raise RuntimeObjectNotFound(
                f"No active run for session {receipt.session_id}",
            )
        run = _require_run(db, binding.active_run_id)
        if receipt.run_id != run.run_id:
            raise RuntimeStateConflict(
                "Receipt run_id does not own this session fence",
            )
        if binding.root_session_id != run.session_id:
            raise RuntimeStateConflict(
                "Receipt Session is not bound to the run root",
            )
        return run, binding, False

    run = _require_run(db, receipt.run_id)
    validate_runtime_interrupted_receipt(
        run=run,
        binding=binding,
        receipt=receipt,
    )
    interrupted_sessions = _validated_interrupted_session_ids(
        dict(run.metadata_json or {}),
    )
    if receipt.session_id in interrupted_sessions:
        return run, binding, True
    current = RunStatus(run.status)
    if current in ACTIVE_RUN_STATUSES and (binding is None or binding.active_run_id != run.run_id):
        raise RuntimeStateConflict(
            "RUN_INTERRUPTED Session fence does not belong to the exact run",
        )
    if current in TERMINAL_RUN_STATUSES and current not in {
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }:
        raise RuntimeStateConflict("Completed run cannot accept RUN_INTERRUPTED")
    return run, binding, False


def validate_tool_result_receipt(receipt: RuntimeReceipt) -> None:
    tool_call_id = receipt.payload.get("tool_call_id")
    state = receipt.payload.get("state")
    if not receipt.reply_id or not isinstance(tool_call_id, str) or not tool_call_id:
        raise RuntimeStateConflict(
            "TOOL_RESULT_END is missing reply_id or tool_call_id",
        )
    if state not in {"success", "error", "interrupted", "denied", "running"}:
        raise RuntimeStateConflict("TOOL_RESULT_END has an invalid state")
