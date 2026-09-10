from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.agent_admission import AgentMaintenanceActiveError, claim_runtime_admission
from app.runtime.errors import RuntimeUnavailableError
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from ._store_support import (
    RuntimeInputRejected,
    RuntimeObjectNotFound,
    RuntimeStateConflict,
    _append_json_id,
    _canonical_json,
    _error_payload,
    _finish_run,
    _new_trace_id,
    _remove_json_id,
    _require_run,
    _run_response,
    _transition,
    _validated_reply_ids,
    _validated_team_generation,
)
from .contracts import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AgentRunResponse,
    ConfirmationScope,
    RunStatus,
    RuntimeReceipt,
    confirmation_reply_id,
    is_confirmation_input,
    validate_no_permission_rules,
)
from .hitl import HITLValidationError, validate_pending_actions
from .models import (
    AgentRunModel,
    RuntimePendingActionModel,
    RuntimeReceiptModel,
    RuntimeSessionBindingModel,
)


@dataclass(frozen=True)
class RuntimeRunAdmission:
    run: AgentRunResponse
    should_trigger_upstream: bool
    replay_response: RuntimeChatReplayResponse | None = None


@dataclass(frozen=True)
class RuntimeChatReplayResponse:
    status_code: int
    body: bytes
    content_type: str


class RuntimeRunStoreMixin:
    Session: sessionmaker

    def begin_run(
        self,
        *,
        session_id: str,
        runtime_agent_id: str,
        input_value: Any,
        alert_id: str | None,
        case_id: str | None,
        metadata: dict[str, object],
        client_operation_id: str | None = None,
        confirmation_scope: ConfirmationScope = ConfirmationScope.ONCE,
        expected_run_id: str | None = None,
    ) -> AgentRunResponse:
        return self.admit_run(
            session_id=session_id,
            runtime_agent_id=runtime_agent_id,
            input_value=input_value,
            alert_id=alert_id,
            case_id=case_id,
            metadata=metadata,
            client_operation_id=client_operation_id,
            confirmation_scope=confirmation_scope,
            expected_run_id=expected_run_id,
        ).run

    def admit_run(
        self,
        *,
        session_id: str,
        runtime_agent_id: str,
        input_value: Any,
        alert_id: str | None,
        case_id: str | None,
        metadata: dict[str, object],
        client_operation_id: str | None,
        confirmation_scope: ConfirmationScope = ConfirmationScope.ONCE,
        expected_run_id: str | None = None,
    ) -> RuntimeRunAdmission:
        _validate_run_input(input_value)

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            binding = db.get(RuntimeSessionBindingModel, session_id)
            if binding is None or binding.runtime_agent_id != runtime_agent_id:
                raise RuntimeObjectNotFound(f"Runtime session not found: {session_id}")
            try:
                claim_runtime_admission(db, agent_id=binding.agent_id)
            except AgentMaintenanceActiveError as exc:
                raise RuntimeUnavailableError(
                    "Agent version maintenance is in progress; retry after restore completes.",
                ) from exc
            if is_confirmation_input(input_value):
                run = self._resume_run(
                    db,
                    binding=binding,
                    input_value=input_value,
                    confirmation_scope=confirmation_scope,
                    expected_run_id=expected_run_id,
                )
                return RuntimeRunAdmission(_run_response(run), True)
            input_fingerprint = _input_fingerprint(input_value)
            existing = _operation_run(db, client_operation_id)
            if existing is not None:
                if (
                    existing.session_id != session_id
                    or existing.runtime_agent_id != runtime_agent_id
                    or existing.input_fingerprint != input_fingerprint
                    or existing.alert_id != alert_id
                    or existing.case_id != case_id
                ):
                    raise RuntimeStateConflict("client_operation_id is bound to another immutable chat request")
                return RuntimeRunAdmission(
                    _run_response(existing),
                    False,
                    _chat_replay_response(existing),
                )
            return self._create_initial_run(
                db,
                binding=binding,
                input_fingerprint=input_fingerprint,
                client_operation_id=client_operation_id,
                alert_id=alert_id,
                case_id=case_id,
                metadata=metadata,
            )

    def _create_initial_run(
        self,
        db: Session,
        *,
        binding: RuntimeSessionBindingModel,
        input_fingerprint: str,
        client_operation_id: str | None,
        alert_id: str | None,
        case_id: str | None,
        metadata: dict[str, object],
    ) -> RuntimeRunAdmission:
        if binding.active_run_id:
            active = db.get(AgentRunModel, binding.active_run_id)
            if active is not None and RunStatus(active.status) in ACTIVE_RUN_STATUSES:
                raise RuntimeStateConflict(f"Session already has active run {active.run_id}")
            binding.active_run_id = None
        now = utc_now()
        governed_metadata = dict(metadata)
        governed_metadata.pop("client_operation_id", None)
        run = AgentRunModel(
            run_id=f"run-{uuid.uuid4()}",
            session_id=binding.session_id,
            agent_id=binding.agent_id,
            agent_version_id=binding.agent_version_id,
            runtime_agent_id=binding.runtime_agent_id,
            harness_digest=binding.harness_digest,
            client_operation_id=client_operation_id,
            input_fingerprint=input_fingerprint,
            status=RunStatus.QUEUED.value,
            trace_id=_new_trace_id(),
            alert_id=alert_id,
            case_id=case_id,
            metadata_json=governed_metadata,
            created_at=now,
            updated_at=now,
        )
        db.add(run)
        db.flush()
        binding.active_run_id = run.run_id
        binding.updated_at = now
        return RuntimeRunAdmission(_run_response(run), True)

    def _resume_run(
        self,
        db: Session,
        *,
        binding: RuntimeSessionBindingModel,
        input_value: Any,
        confirmation_scope: ConfirmationScope,
        expected_run_id: str | None,
    ) -> AgentRunModel:
        run = db.get(AgentRunModel, binding.active_run_id) if binding.active_run_id else None
        if run is None or RunStatus(run.status) not in {RunStatus.WAITING_HUMAN, RunStatus.WAITING_EXTERNAL}:
            raise RuntimeStateConflict("Session is not waiting for an external decision")
        if not expected_run_id or expected_run_id != run.run_id:
            raise RuntimeStateConflict("Decision expected_run_id does not match the active run")
        if (run.metadata_json or {}).get("recovery_required") is True:
            raise RuntimeStateConflict("Run recovery must complete before any HITL continuation")
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
        _transition(run, RunStatus.RUNNING)
        run.started_at = run.started_at or utc_now()
        run.updated_at = utc_now()
        return run

    def mark_trigger_started(
        self,
        run_id: str,
        *,
        response_status: int | None = None,
        response_body: bytes | None = None,
        response_content_type: str | None = None,
    ) -> AgentRunResponse:
        response_parts = (response_status, response_body, response_content_type)
        if any(value is None for value in response_parts) and any(value is not None for value in response_parts):
            raise ValueError("Trigger replay response must be stored atomically")
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) == RunStatus.QUEUED:
                _transition(run, RunStatus.RUNNING)
            if response_status is not None and response_body is not None and response_content_type is not None:
                existing_response = (
                    run.trigger_response_status,
                    run.trigger_response_body,
                    run.trigger_response_content_type,
                )
                requested_response = (response_status, response_body, response_content_type)
                if any(value is not None for value in existing_response) and existing_response != requested_response:
                    raise RuntimeStateConflict("Trigger replay response is immutable")
                (
                    run.trigger_response_status,
                    run.trigger_response_body,
                    run.trigger_response_content_type,
                ) = requested_response
            run.started_at = run.started_at or utc_now()
            run.updated_at = utc_now()
            return _run_response(run)

    def fail_trigger(self, run_id: str, *, error: dict[str, object]) -> AgentRunResponse:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                return _run_response(run)
            _transition(run, RunStatus.FAILED)
            run.error_json = dict(error)
            run.terminal_reason = "trigger_failed"
            _finish_run(db, run)
            return _run_response(run)

    def mark_trigger_uncertain(
        self,
        run_id: str,
        *,
        error: dict[str, object],
    ) -> AgentRunResponse:
        """上游可能已受理时保留 fence，交给 quiescent recovery 判定。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                return _run_response(run)
            if RunStatus(run.status) == RunStatus.QUEUED:
                _transition(run, RunStatus.RUNNING)
            metadata = dict(run.metadata_json or {})
            metadata.update(
                {
                    "recovery_required": True,
                    "trigger_uncertain": True,
                },
            )
            run.metadata_json = metadata
            run.error_json = dict(error)
            run.trace_status = "incomplete"
            run.started_at = run.started_at or utc_now()
            run.updated_at = utc_now()
            return _run_response(run)

    def mark_cancel_requested(self, run_id: str) -> AgentRunResponse:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            binding = db.get(RuntimeSessionBindingModel, run.session_id)
            if binding is None or binding.active_run_id != run.run_id or RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                raise RuntimeStateConflict("Only the Session's exact active run can be cancelled")
            metadata = dict(run.metadata_json or {})
            metadata["cancellation_requested"] = True
            run.metadata_json = metadata
            run.updated_at = utc_now()
            return _run_response(run)

    def mark_cancellation_uncertain(
        self,
        run_id: str,
        *,
        error: dict[str, object],
    ) -> AgentRunResponse:
        """任一 Team interrupt 结果不确定时保留全部 fences 等待 reconcile。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                return _run_response(run)
            metadata = dict(run.metadata_json or {})
            metadata.update(
                {
                    "cancellation_requested": True,
                    "cancellation_uncertain": True,
                    "recovery_required": True,
                },
            )
            run.metadata_json = metadata
            run.error_json = _error_payload(error)
            run.trace_status = "incomplete"
            run.updated_at = utc_now()
            return _run_response(run)

    def apply_receipt(self, receipt: RuntimeReceipt) -> AgentRunResponse:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            duplicate = db.get(RuntimeReceiptModel, receipt.receipt_id)
            if duplicate is None:
                duplicate = db.scalar(select(RuntimeReceiptModel).where(RuntimeReceiptModel.event_id == receipt.event_id))
            if duplicate is not None:
                return _run_response(_require_run(db, duplicate.run_id))
            binding = db.get(RuntimeSessionBindingModel, receipt.session_id)
            if binding is None or not binding.active_run_id:
                raise RuntimeObjectNotFound(f"No active run for session {receipt.session_id}")
            run = _require_run(db, binding.active_run_id)
            if receipt.run_id and receipt.run_id != run.run_id:
                raise RuntimeStateConflict("Receipt run_id does not own this session fence")
            if binding.root_session_id != run.session_id:
                raise RuntimeStateConflict("Receipt Session is not bound to the run root")
            db.add(
                RuntimeReceiptModel(
                    receipt_id=receipt.receipt_id,
                    event_id=receipt.event_id,
                    run_id=run.run_id,
                    session_id=receipt.session_id,
                    reply_id=receipt.reply_id,
                    event_type=receipt.type,
                    payload_json=dict(receipt.payload),
                )
            )
            if binding.session_id == run.session_id:
                self._apply_event(db, run=run, receipt=receipt)
            else:
                self._apply_child_event(
                    db,
                    run=run,
                    binding=binding,
                    receipt=receipt,
                )
            return _run_response(run)

    def _apply_child_event(
        self,
        db: Session,
        *,
        run: AgentRunModel,
        binding: RuntimeSessionBindingModel,
        receipt: RuntimeReceipt,
    ) -> None:
        """记录 worker 生命周期，但绝不把 worker reply 当作顶层 reply。"""

        if receipt.trace_id and run.trace_id != receipt.trace_id:
            raise RuntimeStateConflict("A run cannot be rebound to another trace")
        if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
            raise RuntimeStateConflict("Terminal run cannot accept new lifecycle events")
        current = RunStatus(run.status)
        if receipt.type == "REPLY_START":
            if current in {
                RunStatus.WAITING_HUMAN,
                RunStatus.WAITING_EXTERNAL,
            }:
                _transition(run, RunStatus.RUNNING)
            run.started_at = run.started_at or utc_now()
        elif receipt.type == "REQUIRE_USER_CONFIRM":
            self._store_pending_actions(
                db,
                run=run,
                receipt=receipt,
                kind="human",
                session_id=binding.session_id,
            )
            _transition(run, RunStatus.WAITING_HUMAN)
        elif receipt.type == "REQUIRE_EXTERNAL_EXECUTION":
            self._store_pending_actions(
                db,
                run=run,
                receipt=receipt,
                kind="external",
                session_id=binding.session_id,
            )
            _transition(run, RunStatus.WAITING_EXTERNAL)
        elif receipt.type == "REPLY_END":
            reason = receipt.payload.get("finished_reason")
            if not receipt.reply_id or not isinstance(reason, str) or not reason:
                raise RuntimeStateConflict("Worker REPLY_END is missing reply_id or finished_reason")
        elif receipt.type == "TOOL_RESULT_END":
            _validate_tool_result_receipt(receipt)
        elif receipt.type == "SESSION_PERSISTED":
            _validated_reply_ids(receipt.payload.get("reply_ids"))
            if receipt.reply_id is not None:
                raise RuntimeStateConflict("SESSION_PERSISTED must not identify one reply")
            generation = _validated_team_generation(
                receipt.payload.get("team_generation"),
            )
            if generation <= 0 or generation > binding.active_team_generation:
                raise RuntimeStateConflict("Worker persistence generation does not match its inbox fence")
            if generation == binding.active_team_generation:
                _remove_json_id(
                    run,
                    "pending_child_session_ids_json",
                    binding.session_id,
                )
            self._maybe_finish_persistence_batch(db, run)
        elif receipt.type == "PERSISTENCE_FAILED":
            # Worker 的 canonical state 未闭合时继续保留 pending fence；API
            # restart reconciliation 会中断该 run，不能猜测 worker 已静止。
            run.trace_status = "incomplete"
        run.updated_at = utc_now()

    def _apply_event(self, db: Session, *, run: AgentRunModel, receipt: RuntimeReceipt) -> None:
        if receipt.trace_id:
            if run.trace_id != receipt.trace_id:
                raise RuntimeStateConflict("A run cannot be rebound to another trace")
        if receipt.trace_url:
            run.trace_url = receipt.trace_url
        event_type = receipt.type
        current = RunStatus(run.status)
        if current in TERMINAL_RUN_STATUSES:
            raise RuntimeStateConflict("Terminal run cannot accept new lifecycle events")
        if receipt.reply_id and event_type in {
            "REPLY_START",
            "REQUIRE_USER_CONFIRM",
            "REQUIRE_EXTERNAL_EXECUTION",
            "REPLY_END",
        }:
            _append_json_id(run, "reply_ids_json", receipt.reply_id)
        if event_type == "REPLY_START":
            if current in {
                RunStatus.QUEUED,
                RunStatus.WAITING_HUMAN,
                RunStatus.WAITING_EXTERNAL,
                RunStatus.FINALIZING,
            }:
                _transition(run, RunStatus.RUNNING)
            run.started_at = run.started_at or utc_now()
        elif event_type == "REQUIRE_USER_CONFIRM":
            self._store_pending_actions(db, run=run, receipt=receipt, kind="human")
            _transition(run, RunStatus.WAITING_HUMAN)
        elif event_type == "REQUIRE_EXTERNAL_EXECUTION":
            self._store_pending_actions(db, run=run, receipt=receipt, kind="external")
            _transition(run, RunStatus.WAITING_EXTERNAL)
        elif event_type == "REPLY_END":
            reason = receipt.payload.get("finished_reason")
            if not receipt.reply_id or not isinstance(reason, str) or not reason:
                raise RuntimeStateConflict("REPLY_END is missing reply_id or finished_reason")
            _transition(run, RunStatus.FINALIZING)
        elif event_type == "TOOL_RESULT_END":
            _validate_tool_result_receipt(receipt)
        elif event_type == "MESSAGE_PERSISTED":
            self._apply_message_persisted(db, run=run, receipt=receipt, current=current)
        elif event_type == "SESSION_PERSISTED":
            self._apply_session_persisted(db, run=run, receipt=receipt, current=current)
        elif event_type == "PERSISTENCE_FAILED":
            self._apply_persistence_failed(db, run=run, receipt=receipt, current=current)
        run.updated_at = utc_now()

    def _apply_message_persisted(
        self,
        db: Session,
        *,
        run: AgentRunModel,
        receipt: RuntimeReceipt,
        current: RunStatus,
    ) -> None:
        if current not in {RunStatus.RUNNING, RunStatus.FINALIZING}:
            raise RuntimeStateConflict("Persistence confirmation requires a finalizing run")
        reason = receipt.payload.get("finished_reason")
        if not receipt.reply_id or not isinstance(reason, str) or not reason:
            raise RuntimeStateConflict("Persistence confirmation is missing reply_id or finished_reason")
        if receipt.payload.get("message_persisted") is not True:
            run.trace_status = "incomplete"
            run.terminal_reason = "observation_incomplete"
            _transition(run, RunStatus.INTERRUPTED)
            run.error_json = _error_payload(receipt.payload.get("error"))
            _finish_run(db, run)
            return
        reply_end = db.scalar(
            select(RuntimeReceiptModel)
            .where(
                RuntimeReceiptModel.run_id == run.run_id,
                RuntimeReceiptModel.reply_id == receipt.reply_id,
                RuntimeReceiptModel.event_type == "REPLY_END",
            )
            .order_by(RuntimeReceiptModel.received_at.desc())
            .limit(1),
        )
        if reply_end is not None and (reply_end.payload_json or {}).get("finished_reason") != reason:
            raise RuntimeStateConflict("Persisted Message does not match the observed REPLY_END")
        _append_json_id(run, "persisted_reply_ids_json", receipt.reply_id)
        self._maybe_finish_persistence_batch(db, run)

    def _apply_session_persisted(
        self,
        db: Session,
        *,
        run: AgentRunModel,
        receipt: RuntimeReceipt,
        current: RunStatus,
    ) -> None:
        reply_ids = _validated_reply_ids(receipt.payload.get("reply_ids"))
        if receipt.reply_id is not None:
            raise RuntimeStateConflict("SESSION_PERSISTED must not identify one reply")
        generation = _validated_team_generation(receipt.payload.get("team_generation"))
        if generation > run.team_generation:
            raise RuntimeStateConflict("Root persistence cannot acknowledge a future Team generation")
        existing = list(run.persistence_batch_reply_ids_json or [])
        run.persistence_batch_reply_ids_json = [
            *existing,
            *(reply_id for reply_id in reply_ids if reply_id not in existing),
        ]
        run.root_persisted_team_generation = max(run.root_persisted_team_generation, generation)
        if current == RunStatus.RUNNING:
            _transition(run, RunStatus.FINALIZING)
        elif current != RunStatus.FINALIZING:
            raise RuntimeStateConflict("Session persistence requires a running or finalizing run")
        self._maybe_finish_persistence_batch(db, run)

    @staticmethod
    def _apply_persistence_failed(
        db: Session,
        *,
        run: AgentRunModel,
        receipt: RuntimeReceipt,
        current: RunStatus,
    ) -> None:
        if current not in {RunStatus.RUNNING, RunStatus.FINALIZING}:
            raise RuntimeStateConflict("Persistence failure cannot close the current run state")
        run.trace_status = "incomplete"
        run.terminal_reason = "observation_incomplete"
        run.error_json = _error_payload(receipt.payload.get("error") or "AgentScope message was not readable after REPLY_END")
        _transition(run, RunStatus.INTERRUPTED)
        _finish_run(db, run)

    def _maybe_finish_persistence_batch(self, db: Session, run: AgentRunModel) -> None:
        """expected、canonical Message 与 Session batch marker 全满足才释放 fence。"""

        if RunStatus(run.status) not in {RunStatus.RUNNING, RunStatus.FINALIZING}:
            return
        marker = list(run.persistence_batch_reply_ids_json or [])
        if not marker:
            return
        if run.pending_child_session_ids_json:
            return
        if run.root_persisted_team_generation < run.team_generation:
            return
        persisted = set(run.persisted_reply_ids_json or [])
        if not set(marker).issubset(persisted):
            return
        expected = list(run.reply_ids_json or [])
        if not expected:
            # setup/assembly failure 的合成 Message 未经过 agent middleware，
            # 因而没有可信 REPLY_END。保留错误但不能把它猜成正常 Runtime 终态。
            run.trace_status = "incomplete"
            run.terminal_reason = "observation_incomplete"
            message_receipt = db.scalar(
                select(RuntimeReceiptModel)
                .where(
                    RuntimeReceiptModel.run_id == run.run_id,
                    RuntimeReceiptModel.reply_id.in_(marker),
                    RuntimeReceiptModel.event_type == "MESSAGE_PERSISTED",
                )
                .order_by(RuntimeReceiptModel.received_at.desc())
                .limit(1)
            )
            if message_receipt is not None:
                run.error_json = _error_payload((message_receipt.payload_json or {}).get("error"))
            _transition(run, RunStatus.INTERRUPTED)
            _finish_run(db, run)
            return
        if set(marker) != set(expected):
            return

        receipts = list(
            db.scalars(
                select(RuntimeReceiptModel).where(
                    RuntimeReceiptModel.run_id == run.run_id,
                    RuntimeReceiptModel.reply_id.in_(expected),
                    RuntimeReceiptModel.event_type.in_(("REPLY_END", "MESSAGE_PERSISTED")),
                ),
            ).all(),
        )
        reply_ends = {row.reply_id: row for row in receipts if row.event_type == "REPLY_END"}
        messages = {row.reply_id: row for row in receipts if row.event_type == "MESSAGE_PERSISTED"}
        if set(reply_ends) != set(expected) or set(messages) != set(expected):
            return
        reasons: list[str] = []
        error: object = None
        for reply_id in expected:
            reply_payload = reply_ends[reply_id].payload_json or {}
            message_payload = messages[reply_id].payload_json or {}
            reason = message_payload.get("finished_reason")
            if not isinstance(reason, str) or reason != reply_payload.get("finished_reason"):
                raise RuntimeStateConflict("Persisted Message does not match the observed REPLY_END")
            reasons.append(reason)
            error = error or message_payload.get("error")

        failed_reason = next((reason for reason in reasons if reason not in {"completed", "interrupted"}), None)
        if failed_reason is not None:
            terminal = RunStatus.FAILED
            terminal_reason = failed_reason
        elif "interrupted" in reasons:
            terminal = RunStatus.CANCELLED if (run.metadata_json or {}).get("cancellation_requested") else RunStatus.INTERRUPTED
            terminal_reason = "interrupted"
        else:
            terminal = RunStatus.SUCCEEDED
            terminal_reason = "completed"
        run.terminal_reason = terminal_reason
        run.error_json = _error_payload(error)
        # Runtime flush 只说明 OTLP 已尝试发送；只有控制面用 durable facts
        # 验证 Langfuse observation graph 后才能把状态提升为 complete。
        run.trace_status = "pending"
        _transition(run, terminal)
        _finish_run(db, run)

    def mark_trace_observed(self, run_id: str, *, trace_url: str | None = None) -> AgentRunResponse:
        """Langfuse 已能读取该 trace 时，将派生观测状态标为完整。"""

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

    def _store_pending_actions(
        self,
        db: Session,
        *,
        run: AgentRunModel,
        receipt: RuntimeReceipt,
        kind: str,
        session_id: str | None = None,
    ) -> None:
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
            if not isinstance(tool_call, dict):
                raise RuntimeStateConflict("HITL tool_call must be an object")
            tool_id = tool_call.get("id")
            tool_name = tool_call.get("name")
            if not isinstance(tool_id, str) or not tool_id or not isinstance(tool_name, str) or not tool_name:
                raise RuntimeStateConflict("HITL tool_call must include stable id and name")
            action_id = f"{run.run_id}:{reply_id}:{tool_id}"
            existing = db.get(RuntimePendingActionModel, action_id)
            if existing is not None:
                if _canonical_json(existing.tool_call_json) != _canonical_json(tool_call):
                    raise RuntimeStateConflict("Pending tool call changed under the same identity")
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
                    tool_call_json=tool_call,
                )
            )


def _input_fingerprint(input_value: Any) -> str:
    try:
        canonical = _canonical_json(input_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeInputRejected("Runtime chat input must be canonical JSON") from exc
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_run_input(input_value: Any) -> None:
    try:
        validate_no_permission_rules(input_value)
    except ValueError as exc:
        raise RuntimeInputRejected(str(exc)) from exc


def _validate_tool_result_receipt(receipt: RuntimeReceipt) -> None:
    tool_call_id = receipt.payload.get("tool_call_id")
    state = receipt.payload.get("state")
    if not receipt.reply_id or not isinstance(tool_call_id, str) or not tool_call_id:
        raise RuntimeStateConflict("TOOL_RESULT_END is missing reply_id or tool_call_id")
    if state not in {"success", "error", "interrupted", "denied", "running"}:
        raise RuntimeStateConflict("TOOL_RESULT_END has an invalid state")


def _operation_run(
    db: Session,
    client_operation_id: str | None,
) -> AgentRunModel | None:
    if client_operation_id is None:
        return None
    rows = list(
        db.scalars(
            select(AgentRunModel).where(
                AgentRunModel.client_operation_id == client_operation_id,
            ),
        ).all(),
    )
    if len(rows) > 1:
        raise RuntimeStateConflict("client_operation_id has multiple AgentGov runs")
    return rows[0] if rows else None


def _chat_replay_response(run: AgentRunModel) -> RuntimeChatReplayResponse | None:
    if run.trigger_response_status is None or run.trigger_response_body is None or not run.trigger_response_content_type:
        return None
    return RuntimeChatReplayResponse(
        status_code=run.trigger_response_status,
        body=run.trigger_response_body,
        content_type=run.trigger_response_content_type,
    )
