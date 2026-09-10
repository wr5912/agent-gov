from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.runtime.json_types import JsonObject

from ._router_operations import (
    _audit_error,
    _compensate_session_creation,
    _request_binding_interrupts,
)
from .client import AgentScopeRuntimeClient, RuntimeUpstreamError
from .contracts import TERMINAL_RUN_STATUSES, AgentRunResponse, RuntimeReceipt
from .models import RuntimeSessionCreationIntentModel
from .store import (
    RuntimeObjectNotFound,
    RuntimeRunStore,
    RuntimeStateConflict,
    SessionCreationStatus,
)

SESSION_CREATION_RECOVERY_AGE_SECONDS = 120


@dataclass
class RuntimeGatewayRecoveryReport:
    session_intents_cleaned: int = 0
    session_intents_bound: int = 0
    runs_finalized: int = 0
    failures: int = 0


async def reconcile_runtime_gateway(
    *,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    include_fresh_intents: bool = False,
) -> RuntimeGatewayRecoveryReport:
    """清理 Session 创建孤儿，并以 canonical Message 补齐丢失终态回执。"""

    report = RuntimeGatewayRecoveryReport()
    cutoff = None
    if not include_fresh_intents:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SESSION_CREATION_RECOVERY_AGE_SECONDS)).isoformat()
    for intent in store.recoverable_session_creations(updated_before=cutoff):
        try:
            outcome = await _reconcile_session_intent(client, store, intent)
            if outcome == "bound":
                report.session_intents_bound += 1
            elif outcome == "cleaned":
                report.session_intents_cleaned += 1
            else:
                report.failures += 1
        except Exception as exc:
            retry_status = SessionCreationStatus.PENDING if intent.session_id is None else SessionCreationStatus.CLEANUP_PENDING
            store.mark_session_creation(intent.intent_id, status=retry_status, error=_audit_error("reconcile_session", exc))
            report.failures += 1
    # lifespan 的首次调用只处理 Session create intent；随后
    # reconcile_after_restart 会先给所有 active run 加 recovery fence。
    if not include_fresh_intents:
        for run in store.finalizing_runs():
            if run.metadata.get("recovery_required") is True:
                continue
            try:
                receipts = await _recover_persistence_receipts(client, run)
                if receipts:
                    for receipt in receipts:
                        store.apply_receipt(receipt)
                    if store.get_run(run.run_id).status in TERMINAL_RUN_STATUSES:
                        report.runs_finalized += 1
            except Exception:
                report.failures += 1
        for run in store.recovery_required_runs():
            try:
                if await _recover_restarted_run(client, store, run):
                    report.runs_finalized += 1
            except Exception:
                report.failures += 1
    return report


async def _reconcile_session_intent(
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    intent: RuntimeSessionCreationIntentModel,
) -> str:
    session_id = intent.session_id
    if session_id is None:
        session_id = await _find_workspace_session(client, intent.runtime_agent_id, intent.workspace_id)
        if session_id is None:
            store.mark_session_creation(
                intent.intent_id,
                status=SessionCreationStatus.FAILED_CLEANED,
                error={"stage": "reconcile_session", "type": "RuntimeSessionMissing"},
            )
            return "cleaned"
        store.record_session_creation_upstream(intent.intent_id, session_id)
    try:
        store.get_session(session_id)
    except RuntimeObjectNotFound:
        cleaned = await _compensate_session_creation(
            client,
            store,
            intent.intent_id,
            session_id,
            intent.runtime_agent_id,
            RuntimeStateConflict("Recovered an unbound Runtime Session"),
        )
        return "cleaned" if cleaned else "pending"
    store.complete_session_creation(intent.intent_id)
    return "bound"


async def _find_workspace_session(client: AgentScopeRuntimeClient, runtime_agent_id: str, workspace_id: str) -> str | None:
    upstream = await client.request_json("GET", "/sessions/", params={"agent_id": runtime_agent_id})
    values = upstream.body.get("sessions") if isinstance(upstream.body, dict) else None
    if not isinstance(values, list):
        raise RuntimeUpstreamError(502, b'{"detail":"Runtime returned invalid Session list"}')
    matches: list[str] = []
    for value in values:
        session = value.get("session") if isinstance(value, dict) else None
        config = session.get("config") if isinstance(session, dict) else None
        if isinstance(config, dict) and config.get("workspace_id") == workspace_id:
            session_id = session.get("id")
            if isinstance(session_id, str) and session_id:
                matches.append(session_id)
    if len(matches) > 1:
        raise RuntimeStateConflict("Runtime returned multiple Sessions for one immutable workspace")
    return matches[0] if matches else None


async def _recover_restarted_run(
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    run: AgentRunResponse,
) -> bool:
    """先 interrupt，再以连续静止观测将重启中的 run fail-closed。"""

    bindings = store.active_session_bindings(run.run_id)
    if not bindings:
        store.fail_recovery(run.run_id, error="Runtime Session fences disappeared during restart recovery")
        return True
    if run.metadata.get("recovery_interrupt_requested") is not True:
        results = await _request_binding_interrupts(client, bindings)
        errors = [result for result in results if isinstance(result, BaseException)]
        unresolved = [error for error in errors if not isinstance(error, RuntimeUpstreamError) or error.status_code != 404]
        if unresolved:
            raise unresolved[0]
        store.mark_recovery_interrupt_requested(run.run_id)
        return False

    for binding in bindings:
        try:
            status_response = await client.request_json(
                "GET",
                f"/sessions/{binding.session_id}/status",
                params={"agent_id": binding.runtime_agent_id},
            )
        except RuntimeUpstreamError as exc:
            if exc.status_code == 404:
                # 该 Session 已不存在等价于自身静止；仍须逐一确认其余
                # Team Sessions idle，不能提前释放整个 run 的 fences。
                continue
            store.reset_recovery_quiescent(run.run_id)
            raise
        status = status_response.body.get("status") if isinstance(status_response.body, dict) else None
        if status != "idle":
            store.reset_recovery_quiescent(run.run_id)
            return False

    if store.note_recovery_quiescent(run.run_id) >= 2:
        store.fail_recovery(
            run.run_id,
            error="Runtime restarted while the run was active; all bound Sessions are now idle",
        )
        return True
    return False


async def _recover_persistence_receipts(
    client: AgentScopeRuntimeClient,
    run: AgentRunResponse,
) -> list[RuntimeReceipt] | None:
    try:
        upstream = await client.request_json(
            "GET",
            f"/sessions/{run.session_id}/messages",
            params={"agent_id": run.runtime_agent_id, "limit": 200},
        )
    except RuntimeUpstreamError as exc:
        if exc.status_code == 404:
            return [_reconciled_receipt(run, "PERSISTENCE_FAILED", None)]
        raise
    messages = upstream.body.get("messages") if isinstance(upstream.body, dict) else None
    if not isinstance(messages, list):
        raise RuntimeUpstreamError(502, b'{"detail":"Runtime returned invalid Message list"}')
    reply_ids = list(run.reply_ids)
    if not reply_ids:
        return None
    expected = set(reply_ids)
    readable: dict[str, JsonObject] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("id") not in expected:
            continue
        if isinstance(message.get("finished_reason"), str) and message["finished_reason"]:
            readable[str(message["id"])] = message
    if set(readable) != expected:
        return None
    receipts = [_reconciled_receipt(run, "MESSAGE_PERSISTED", readable[reply_id]) for reply_id in reply_ids if reply_id not in set(run.persisted_reply_ids)]
    if set(run.persistence_batch_reply_ids) != expected:
        receipts.append(_reconciled_batch_receipt(run, reply_ids))
    return receipts


def _reconciled_receipt(run: AgentRunResponse, event_type: str, message: JsonObject | None) -> RuntimeReceipt:
    message_id = message.get("id") if message else None
    identity = f"runtime-reconcile\n{event_type}\n{run.run_id}\n{message_id or ''}"
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    receipt_data: JsonObject
    if message is None:
        receipt_data = {"error": {"type": "runtime_session_missing"}}
    else:
        error = message.get("error")
        error_type = error.get("type") if isinstance(error, dict) else None
        receipt_data = {
            "message_id": message_id,
            "message_persisted": True,
            "finished_reason": message["finished_reason"],
            "error": {"type": error_type} if isinstance(error_type, str) else None,
            "trace_complete": False,
            "reconciled": True,
        }
    return RuntimeReceipt(
        receipt_id=hashlib.sha256(f"receipt\n{identity}".encode()).hexdigest(),
        event_id=event_id,
        session_id=run.session_id,
        run_id=run.run_id,
        reply_id=message_id if isinstance(message_id, str) else None,
        type=event_type,
        payload=receipt_data,
        trace_id=run.trace_id,
    )


def _reconciled_batch_receipt(run: AgentRunResponse, reply_ids: list[str]) -> RuntimeReceipt:
    identity = "\n".join(("runtime-reconcile", "SESSION_PERSISTED", run.run_id, *reply_ids))
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    return RuntimeReceipt(
        receipt_id=hashlib.sha256(f"receipt\n{identity}".encode()).hexdigest(),
        event_id=event_id,
        session_id=run.session_id,
        run_id=run.run_id,
        reply_id=None,
        type="SESSION_PERSISTED",
        payload={
            "reply_ids": reply_ids,
            "message_count": len(reply_ids),
            "team_generation": run.team_generation,
            "reconciled": True,
        },
        trace_id=run.trace_id,
    )
