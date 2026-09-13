from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from ._store_support import (
    RuntimeObjectNotFound,
    RuntimeStateConflict,
    _detached,
    _require_run,
    _run_response,
    _runtime_context_response,
)
from .contracts import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AgentRunResponse,
    RunStatus,
    RuntimeContextResponse,
    RuntimePendingActionResponse,
    RuntimeToolResultState,
    RuntimeTraceActionExpectation,
    RuntimeTraceActionStatus,
    RuntimeTraceExpectations,
    RuntimeTraceTeamChildExpectation,
    RuntimeTraceToolExpectation,
)
from .hitl import HITLValidationError, parse_tool_call_fingerprint
from .models import (
    AgentRunModel,
    RuntimeChatOperationModel,
    RuntimePendingActionModel,
    RuntimeReceiptModel,
    RuntimeSessionBindingModel,
    RuntimeTeamDeliveryModel,
)


class RuntimeRunQueryStoreMixin:
    Session: sessionmaker

    def runtime_context(self, session_id: str) -> RuntimeContextResponse:
        with self.Session() as db:
            binding = db.get(RuntimeSessionBindingModel, session_id)
            if binding is None or not binding.active_run_id:
                raise RuntimeObjectNotFound(f"No active run for session {session_id}")
            run = _require_run(db, binding.active_run_id)
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                raise RuntimeObjectNotFound(f"No active run for session {session_id}")
            if binding.root_session_id != run.session_id:
                raise RuntimeStateConflict("Runtime Session binding does not match its run root")
            return _runtime_context_response(run, binding)

    def get_run(self, run_id: str) -> AgentRunResponse:
        with self.Session() as db:
            return _run_response(_require_run(db, run_id))

    def run_for_client_operation(
        self,
        *,
        session_id: str,
        client_operation_id: str,
    ) -> AgentRunResponse:
        with self.Session() as db:
            operation_run_ids = set(
                db.scalars(
                    select(RuntimeChatOperationModel.run_id).where(
                        RuntimeChatOperationModel.root_session_id == session_id,
                        RuntimeChatOperationModel.client_operation_id == client_operation_id,
                    ),
                ).all(),
            )
            origin_run_ids = set(
                db.scalars(
                    select(AgentRunModel.run_id).where(
                        AgentRunModel.session_id == session_id,
                        AgentRunModel.client_operation_id == client_operation_id,
                    ),
                ).all(),
            )
            run_ids = operation_run_ids | origin_run_ids
            if not run_ids:
                raise RuntimeObjectNotFound("Agent run not found for client operation")
            if len(run_ids) > 1:
                raise RuntimeStateConflict(
                    "client_operation_id has multiple AgentGov runs",
                )
            if not operation_run_ids:
                raise RuntimeStateConflict(
                    "client_operation_id run exists without its durable operation ledger",
                )
            return _run_response(_require_run(db, run_ids.pop()))

    def active_run_for_session(self, session_id: str) -> AgentRunResponse | None:
        with self.Session() as db:
            binding = db.get(RuntimeSessionBindingModel, session_id)
            if binding is None or not binding.active_run_id:
                return None
            run = db.get(AgentRunModel, binding.active_run_id)
            return _run_response(run) if run is not None else None

    def pending_actions_for_run(self, run_id: str) -> list[RuntimePendingActionResponse]:
        with self.Session() as db:
            _require_run(db, run_id)
            rows = db.scalars(
                select(RuntimePendingActionModel)
                .where(
                    RuntimePendingActionModel.run_id == run_id,
                    RuntimePendingActionModel.status == "pending",
                )
                .order_by(
                    RuntimePendingActionModel.created_at,
                    RuntimePendingActionModel.action_id,
                ),
            ).all()
            return [_pending_action_response(row) for row in rows]

    def trace_expectations(self, run_id: str) -> RuntimeTraceExpectations:
        """只用 durable control-plane facts 生成 Trace 验收预期。"""

        with self.Session() as db:
            run = _require_run(db, run_id)
            receipts = list(
                db.scalars(
                    select(RuntimeReceiptModel)
                    .where(RuntimeReceiptModel.run_id == run_id)
                    .order_by(RuntimeReceiptModel.received_at, RuntimeReceiptModel.receipt_id),
                ).all(),
            )
            actions = list(
                db.scalars(
                    select(RuntimePendingActionModel)
                    .where(RuntimePendingActionModel.run_id == run_id)
                    .order_by(RuntimePendingActionModel.created_at, RuntimePendingActionModel.action_id),
                ).all(),
            )
            deliveries = list(
                db.scalars(
                    select(RuntimeTeamDeliveryModel)
                    .where(RuntimeTeamDeliveryModel.run_id == run_id)
                    .order_by(RuntimeTeamDeliveryModel.generation, RuntimeTeamDeliveryModel.event_id),
                ).all(),
            )
            return _trace_expectations(run, receipts, actions, deliveries, db)

    def pending_terminal_traces(self, *, limit: int = 100) -> list[AgentRunResponse]:
        """列出需要后台与 Langfuse 对账的终态 runs。"""

        if limit <= 0:
            raise ValueError("Trace reconciliation limit must be positive")
        with self.Session() as db:
            rows = db.scalars(
                select(AgentRunModel)
                .where(
                    AgentRunModel.status.in_([item.value for item in TERMINAL_RUN_STATUSES]),
                    AgentRunModel.trace_status == "pending",
                    AgentRunModel.trace_id.is_not(None),
                )
                .order_by(AgentRunModel.completed_at, AgentRunModel.run_id)
                .limit(limit),
            ).all()
            return [_run_response(row) for row in rows]

    def mark_trace_observed(
        self,
        run_id: str,
        *,
        trace_url: str | None = None,
    ) -> AgentRunResponse:
        """Langfuse 派生图已与 durable facts 对账时提升为完整。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) not in TERMINAL_RUN_STATUSES:
                raise RuntimeStateConflict("Only a terminal run can have a complete trace")
            run.trace_status = "complete"
            if trace_url:
                run.trace_url = trace_url
            run.updated_at = utc_now()
            return _run_response(run)

    def mark_trace_incomplete(self, run_id: str) -> AgentRunResponse:
        """Trace 在有界等待后仍与 durable facts 不完整。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) not in TERMINAL_RUN_STATUSES:
                raise RuntimeStateConflict("Only a terminal run can have an incomplete trace")
            if run.trace_status == "pending":
                run.trace_status = "incomplete"
                run.updated_at = utc_now()
            return _run_response(run)

    def active_run_for_agent(self, agent_id: str) -> AgentRunResponse | None:
        with self.Session() as db:
            run = db.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.agent_id == agent_id, AgentRunModel.status.in_([item.value for item in ACTIVE_RUN_STATUSES]))
                .order_by(AgentRunModel.created_at)
                .limit(1)
            )
            return _run_response(run) if run is not None else None

    def active_session_bindings(self, run_id: str) -> list[RuntimeSessionBindingModel]:
        """列出顶层与 Team child 的全部 active Session fences。"""

        with self.Session() as db:
            rows = db.scalars(
                select(RuntimeSessionBindingModel).where(RuntimeSessionBindingModel.active_run_id == run_id).order_by(RuntimeSessionBindingModel.session_id),
            ).all()
            return [_detached(db, row) for row in rows]

    def finalizing_runs(self) -> list[AgentRunResponse]:
        """返回需要从 AgentScope canonical Message 恢复终态的 runs。"""

        with self.Session() as db:
            rows = db.scalars(
                select(AgentRunModel).where(AgentRunModel.status == RunStatus.FINALIZING.value).order_by(AgentRunModel.updated_at),
            ).all()
            return [_run_response(row) for row in rows]


def _pending_action_response(
    row: RuntimePendingActionModel,
) -> RuntimePendingActionResponse:
    if row.kind not in {"human", "external"}:
        raise RuntimeStateConflict("Pending action kind is invalid")
    try:
        fingerprint = parse_tool_call_fingerprint(
            row.tool_call_json,
            expected_id=row.tool_call_id,
            expected_name=row.tool_call_name,
        )
    except HITLValidationError as exc:
        raise RuntimeStateConflict(str(exc)) from exc
    return RuntimePendingActionResponse(
        action_id=row.action_id,
        session_id=row.session_id,
        run_id=row.run_id,
        reply_id=row.reply_id,
        kind="human" if row.kind == "human" else "external",
        **fingerprint.model_dump(mode="python"),
        status="pending",
        created_at=row.created_at,
    )


_TRACE_ACTION_STATUSES = frozenset({"pending", "resolved", "expired"})
_TRACE_TOOL_STATES = frozenset({"success", "error", "interrupted", "denied", "running"})


def _trace_expectations(
    run: AgentRunModel,
    receipts: list[RuntimeReceiptModel],
    actions: list[RuntimePendingActionModel],
    deliveries: list[RuntimeTeamDeliveryModel],
    db: Session,
) -> RuntimeTraceExpectations:
    root_reply_ids, integrity_complete = _trace_root_reply_ids(run.reply_ids_json)
    interrupted_before_reply, interruption_integrity = _trace_interruption_expectation(
        run,
        receipts,
        root_reply_ids,
    )
    action_expectations: list[RuntimeTraceActionExpectation] = []
    for action in actions:
        if (
            action.kind not in {"human", "external"}
            or action.status not in _TRACE_ACTION_STATUSES
            or not action.session_id
            or not action.reply_id
            or not action.tool_call_id
        ):
            integrity_complete = False
            continue
        action_expectations.append(
            RuntimeTraceActionExpectation(
                session_id=action.session_id,
                reply_id=action.reply_id,
                tool_call_id=action.tool_call_id,
                kind="human" if action.kind == "human" else "external",
                status=cast(RuntimeTraceActionStatus, action.status),
            ),
        )
    if RunStatus(run.status) in TERMINAL_RUN_STATUSES and any(action.status == "pending" for action in action_expectations):
        integrity_complete = False

    tool_expectations, tools_complete = _trace_tool_expectations(receipts, action_expectations)
    expected_generations = list(range(1, run.team_generation + 1))
    if [delivery.generation for delivery in deliveries] != expected_generations:
        integrity_complete = False
    child_ids = _trace_child_session_ids(run.session_id, receipts, action_expectations, deliveries)
    bindings = {
        row.session_id: row
        for row in db.scalars(
            select(RuntimeSessionBindingModel).where(RuntimeSessionBindingModel.session_id.in_(child_ids)),
        ).all()
    }
    children: list[RuntimeTraceTeamChildExpectation] = []
    for session_id in sorted(child_ids):
        binding = bindings.get(session_id)
        valid_binding = (
            binding is not None
            and binding.root_session_id == run.session_id
            and binding.team_id is not None
            and binding.agent_id == run.agent_id
            and binding.agent_version_id == run.agent_version_id
            and binding.harness_digest == run.harness_digest
        )
        integrity_complete = integrity_complete and valid_binding
        children.append(
            RuntimeTraceTeamChildExpectation(
                session_id=session_id,
                runtime_agent_id=binding.runtime_agent_id if valid_binding and binding is not None else None,
            ),
        )
    return RuntimeTraceExpectations(
        run_id=run.run_id,
        root_session_id=run.session_id,
        root_reply_ids=root_reply_ids,
        team_children=children,
        tool_results=tool_expectations,
        actions=action_expectations,
        interrupted_before_reply=interrupted_before_reply,
        control_integrity_complete=(integrity_complete and tools_complete and interruption_integrity),
    )


def _trace_root_reply_ids(raw_reply_ids: object) -> tuple[list[str], bool]:
    if not isinstance(raw_reply_ids, list):
        return [], False
    reply_ids = [value for value in raw_reply_ids if isinstance(value, str) and value and value != "pending"]
    complete = len(reply_ids) == len(raw_reply_ids) and len(set(reply_ids)) == len(reply_ids)
    return reply_ids, complete


def _trace_interruption_expectation(
    run: AgentRunModel,
    receipts: list[RuntimeReceiptModel],
    root_reply_ids: list[str],
) -> tuple[bool, bool]:
    interrupted = [row for row in receipts if row.event_type == "RUN_INTERRUPTED"]
    receipt_sessions = [row.session_id for row in interrupted]
    raw_metadata_sessions = (run.metadata_json or {}).get(
        "runtime_interrupted_session_ids",
    )
    metadata_sessions = raw_metadata_sessions if isinstance(raw_metadata_sessions, list) else []
    metadata_is_valid = (
        (raw_metadata_sessions is None or isinstance(raw_metadata_sessions, list))
        and all(isinstance(value, str) and value for value in metadata_sessions)
        and len(set(metadata_sessions)) == len(metadata_sessions)
    )
    receipt_is_valid = all(row.reply_id is None and not row.payload_json for row in interrupted) and len(set(receipt_sessions)) == len(receipt_sessions)
    integrity_complete = metadata_is_valid and receipt_is_valid and set(metadata_sessions) == set(receipt_sessions)
    interrupted_before_reply = integrity_complete and run.terminal_reason == "interrupted" and not root_reply_ids and run.session_id in receipt_sessions
    return interrupted_before_reply, integrity_complete


def _trace_child_session_ids(
    root_session_id: str,
    receipts: list[RuntimeReceiptModel],
    actions: list[RuntimeTraceActionExpectation],
    deliveries: list[RuntimeTeamDeliveryModel],
) -> set[str]:
    session_ids = {receipt.session_id for receipt in receipts}
    session_ids.update(action.session_id for action in actions)
    for delivery in deliveries:
        session_ids.update((delivery.source_session_id, delivery.target_session_id))
    session_ids.discard(root_session_id)
    return session_ids


def _trace_tool_expectations(
    receipts: list[RuntimeReceiptModel],
    actions: list[RuntimeTraceActionExpectation],
) -> tuple[list[RuntimeTraceToolExpectation], bool]:
    complete = True
    expected: dict[tuple[str, str, str], RuntimeTraceToolExpectation] = {}
    for receipt in receipts:
        if receipt.event_type != "TOOL_RESULT_END":
            continue
        tool_call_id = (receipt.payload_json or {}).get("tool_call_id")
        state = (receipt.payload_json or {}).get("state")
        if not receipt.reply_id or not isinstance(tool_call_id, str) or not tool_call_id or state not in _TRACE_TOOL_STATES:
            complete = False
            continue
        key = (receipt.session_id, receipt.reply_id, tool_call_id)
        candidate = RuntimeTraceToolExpectation(
            session_id=receipt.session_id,
            reply_id=receipt.reply_id,
            tool_call_id=tool_call_id,
            state=cast(RuntimeToolResultState, state),
            source="tool_result_receipt",
        )
        if key in expected and expected[key] != candidate:
            complete = False
        expected[key] = candidate
    for action in actions:
        if action.kind != "external" or action.status != "resolved":
            continue
        key = (action.session_id, action.reply_id, action.tool_call_id)
        # AgentScope 在 continuation 进入 reply middleware 前已结束
        # external synthetic execute_tool span，因此 reply/state 只能由
        # 其父 invoke + durable TOOL_RESULT_END 联合对账。
        expected[key] = RuntimeTraceToolExpectation(
            session_id=action.session_id,
            reply_id=action.reply_id,
            tool_call_id=action.tool_call_id,
            state=None,
            source="external_action",
        )
    return sorted(expected.values(), key=lambda item: (item.session_id, item.reply_id, item.tool_call_id)), complete
