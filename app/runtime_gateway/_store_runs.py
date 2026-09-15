from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.agent_admission import AgentMaintenanceActiveError, claim_runtime_admission
from app.runtime.errors import RuntimeUnavailableError
from app.runtime.feedback_entities import FeedbackEntities
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from ._store_continuations import require_continuation_run, validate_new_continuation
from ._store_operations import (
    RuntimeChatReplayResponse,
    add_continuation_operation,
    add_initial_operation,
    governed_run_metadata,
    native_request_fingerprint,
    operation_replay_response,
    resolve_continuation_identity,
    validate_continuation_operation,
    validate_initial_operation,
)
from ._store_pending_actions import store_pending_actions
from ._store_receipt_validation import (
    resolve_receipt_run,
    validate_duplicate_receipt,
    validate_tool_result_receipt,
)
from ._store_run_recovery import apply_runtime_interrupted_receipt
from ._store_support import (
    RuntimeInputRejected,
    RuntimeObjectNotFound,
    RuntimeStateConflict,
    _append_json_id,
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
    is_confirmation_input,
    validate_no_permission_rules,
)
from .hitl import (
    HITLValidationError,
    validate_runtime_receipt_fingerprints,
)
from .models import (
    AgentRunModel,
    RuntimeChatOperationModel,
    RuntimeReceiptModel,
    RuntimeSessionBindingModel,
)
from .native_chat_input import explicit_native_input_ids, native_operation_key, native_operation_kind


@dataclass(frozen=True)
class RuntimeRunAdmission:
    run: AgentRunResponse
    should_trigger_upstream: bool
    operation_key: str | None
    replay_response: RuntimeChatReplayResponse | None = None


class RuntimeRunStoreMixin:
    Session: sessionmaker

    def begin_run(
        self,
        *,
        session_id: str,
        runtime_agent_id: str,
        input_value: Any,
        entities: FeedbackEntities,
        metadata: dict[str, object],
        confirmation_scope: ConfirmationScope = ConfirmationScope.ONCE,
    ) -> AgentRunResponse:
        return self.admit_run(
            session_id=session_id,
            runtime_agent_id=runtime_agent_id,
            input_value=input_value,
            entities=entities,
            metadata=metadata,
            confirmation_scope=confirmation_scope,
        ).run

    def admit_run(
        self,
        *,
        session_id: str,
        runtime_agent_id: str,
        input_value: Any,
        entities: FeedbackEntities,
        metadata: dict[str, object],
        confirmation_scope: ConfirmationScope = ConfirmationScope.ONCE,
    ) -> RuntimeRunAdmission:
        _validate_run_input(input_value)

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            binding = _require_admission_binding(
                db,
                session_id=session_id,
                runtime_agent_id=runtime_agent_id,
            )
            governed_metadata = governed_run_metadata(metadata)
            input_ids = explicit_native_input_ids(input_value)
            operation_kind = native_operation_kind(input_value)
            operation_key = (
                native_operation_key(
                    runtime_agent_id=runtime_agent_id,
                    session_id=session_id,
                    operation_kind=operation_kind,
                    input_ids=input_ids,
                )
                if input_ids is not None
                else f"native-unkeyed:{uuid.uuid4()}"
            )
            existing_operation = db.get(RuntimeChatOperationModel, operation_key) if input_ids is not None else None
            if is_confirmation_input(input_value):
                return self._resume_run(
                    db,
                    binding=binding,
                    input_value=input_value,
                    metadata=governed_metadata,
                    entities=entities,
                    confirmation_scope=confirmation_scope,
                    operation_key=operation_key,
                    existing_operation=existing_operation,
                )
            request_fingerprint = native_request_fingerprint(
                input_value=input_value,
                metadata=governed_metadata,
                session_id=session_id,
                runtime_agent_id=runtime_agent_id,
                entities=entities,
                confirmation_scope=confirmation_scope,
            )
            if existing_operation is not None:
                existing = _require_run(db, existing_operation.run_id)
                validate_initial_operation(
                    existing_operation,
                    operation_key=operation_key,
                    request_fingerprint=request_fingerprint,
                    session_id=session_id,
                    runtime_agent_id=runtime_agent_id,
                )
                return RuntimeRunAdmission(
                    _run_response(existing),
                    False,
                    existing_operation.operation_key,
                    operation_replay_response(existing_operation),
                )
            return self._create_initial_run(
                db,
                binding=binding,
                request_fingerprint=request_fingerprint,
                operation_key=operation_key,
                entities=entities,
                metadata=governed_metadata,
            )

    def _create_initial_run(
        self,
        db: Session,
        *,
        binding: RuntimeSessionBindingModel,
        request_fingerprint: str,
        operation_key: str,
        entities: FeedbackEntities,
        metadata: dict[str, object],
    ) -> RuntimeRunAdmission:
        if binding.active_run_id:
            active = db.get(AgentRunModel, binding.active_run_id)
            if active is not None and RunStatus(active.status) in ACTIVE_RUN_STATUSES:
                raise RuntimeStateConflict(f"Session already has active run {active.run_id}")
            binding.active_run_id = None
        now = utc_now()
        run = AgentRunModel(
            run_id=f"run-{uuid.uuid4()}",
            session_id=binding.session_id,
            agent_id=binding.agent_id,
            agent_version_id=binding.agent_version_id,
            runtime_agent_id=binding.runtime_agent_id,
            harness_digest=binding.harness_digest,
            status=RunStatus.QUEUED.value,
            trace_id=_new_trace_id(),
            entities_json=entities,
            metadata_json=metadata,
            created_at=now,
            updated_at=now,
        )
        db.add(run)
        db.flush()
        add_initial_operation(
            db,
            run=run,
            operation_key=operation_key,
            request_fingerprint=request_fingerprint,
        )
        binding.active_run_id = run.run_id
        binding.updated_at = now
        return RuntimeRunAdmission(
            _run_response(run),
            True,
            operation_key,
        )

    def _resume_run(
        self,
        db: Session,
        *,
        binding: RuntimeSessionBindingModel,
        input_value: Any,
        metadata: dict[str, object],
        entities: FeedbackEntities,
        confirmation_scope: ConfirmationScope,
        operation_key: str,
        existing_operation: RuntimeChatOperationModel | None,
    ) -> RuntimeRunAdmission:
        run = (
            _require_run(db, existing_operation.run_id)
            if existing_operation is not None
            else require_continuation_run(db, binding=binding, input_value=input_value)
        )
        if run.session_id != binding.root_session_id or run.agent_id != binding.agent_id:
            raise RuntimeStateConflict("Decision does not match the governed root Session")
        identity = resolve_continuation_identity(
            db,
            run=run,
            input_value=input_value,
            metadata=metadata,
            session_id=binding.session_id,
            runtime_agent_id=binding.runtime_agent_id,
            entities=entities,
            operation_key=operation_key,
            confirmation_scope=confirmation_scope,
        )
        if existing_operation is not None:
            validate_continuation_operation(
                existing_operation,
                run=run,
                confirmation_scope=confirmation_scope,
                identity=identity,
            )
            return RuntimeRunAdmission(
                _run_response(run),
                False,
                existing_operation.operation_key,
                operation_replay_response(existing_operation),
            )
        validate_new_continuation(
            db,
            binding=binding,
            run=run,
            input_value=input_value,
            confirmation_scope=confirmation_scope,
        )
        _transition(run, RunStatus.RUNNING)
        run.started_at = run.started_at or utc_now()
        run.updated_at = utc_now()
        add_continuation_operation(
            db,
            run=run,
            confirmation_scope=confirmation_scope,
            identity=identity,
        )
        return RuntimeRunAdmission(
            _run_response(run),
            True,
            identity.operation_key,
        )

    def mark_trigger_started(
        self,
        run_id: str,
    ) -> AgentRunResponse:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) == RunStatus.QUEUED:
                _transition(run, RunStatus.RUNNING)
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

    def apply_receipt(self, receipt: RuntimeReceipt) -> AgentRunResponse:
        try:
            receipt = validate_runtime_receipt_fingerprints(receipt)
        except HITLValidationError as exc:
            raise RuntimeStateConflict(str(exc)) from exc
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            duplicate = db.get(RuntimeReceiptModel, receipt.receipt_id)
            if duplicate is None:
                duplicate = db.scalar(select(RuntimeReceiptModel).where(RuntimeReceiptModel.event_id == receipt.event_id))
            if duplicate is not None:
                duplicate_run = _require_run(db, duplicate.run_id)
                validate_duplicate_receipt(
                    duplicate,
                    receipt,
                    duplicate_run,
                )
                return _run_response(duplicate_run)
            run, binding, interruption_already_recorded = resolve_receipt_run(
                db,
                receipt,
            )
            if interruption_already_recorded:
                return _run_response(run)
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
            if receipt.type == "RUN_INTERRUPTED":
                apply_runtime_interrupted_receipt(
                    run=run,
                    binding=binding,
                    receipt=receipt,
                )
            elif binding is not None and binding.session_id == run.session_id:
                self._apply_event(db, run=run, receipt=receipt)
            elif binding is not None:
                self._apply_child_event(
                    db,
                    run=run,
                    binding=binding,
                    receipt=receipt,
                )
            else:  # pragma: no cover - resolve_receipt_run 已保证普通回执绑定存在
                raise RuntimeStateConflict("Receipt Session binding disappeared")
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

        _validate_active_receipt_run(run, receipt)
        current = RunStatus(run.status)
        if receipt.type == "REPLY_START":
            if current in {
                RunStatus.WAITING_HUMAN,
                RunStatus.WAITING_EXTERNAL,
            }:
                _transition(run, RunStatus.RUNNING)
            run.started_at = run.started_at or utc_now()
        elif receipt.type == "REQUIRE_USER_CONFIRM":
            store_pending_actions(
                db,
                run=run,
                receipt=receipt,
                kind="human",
                session_id=binding.session_id,
            )
            _transition(run, RunStatus.WAITING_HUMAN)
        elif receipt.type == "REQUIRE_EXTERNAL_EXECUTION":
            store_pending_actions(
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
            validate_tool_result_receipt(receipt)
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
            if not _recovery_blocks_terminal(run):
                self._maybe_finish_persistence_batch(db, run)
        elif receipt.type == "PERSISTENCE_FAILED":
            # Worker 的 canonical state 未闭合时继续保留 pending fence；API
            # restart reconciliation 会中断该 run，不能猜测 worker 已静止。
            run.trace_status = "incomplete"
        run.updated_at = utc_now()

    def _apply_event(self, db: Session, *, run: AgentRunModel, receipt: RuntimeReceipt) -> None:
        _validate_active_receipt_run(run, receipt)
        if receipt.trace_url:
            run.trace_url = receipt.trace_url
        event_type = receipt.type
        current = RunStatus(run.status)
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
            store_pending_actions(db, run=run, receipt=receipt, kind="human")
            _transition(run, RunStatus.WAITING_HUMAN)
        elif event_type == "REQUIRE_EXTERNAL_EXECUTION":
            store_pending_actions(db, run=run, receipt=receipt, kind="external")
            _transition(run, RunStatus.WAITING_EXTERNAL)
        elif event_type == "REPLY_END":
            reason = receipt.payload.get("finished_reason")
            if not receipt.reply_id or not isinstance(reason, str) or not reason:
                raise RuntimeStateConflict("REPLY_END is missing reply_id or finished_reason")
            _transition(run, RunStatus.FINALIZING)
        elif event_type == "TOOL_RESULT_END":
            validate_tool_result_receipt(receipt)
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
            if _recovery_blocks_terminal(run):
                return
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
        if not _recovery_blocks_terminal(run):
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
        if not _recovery_blocks_terminal(run):
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
        if _recovery_blocks_terminal(run):
            return
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


def _require_admission_binding(
    db: Session,
    *,
    session_id: str,
    runtime_agent_id: str,
) -> RuntimeSessionBindingModel:
    binding = db.get(RuntimeSessionBindingModel, session_id)
    if binding is None or binding.runtime_agent_id != runtime_agent_id:
        raise RuntimeObjectNotFound(f"Runtime session not found: {session_id}")
    try:
        claim_runtime_admission(db, agent_id=binding.agent_id)
    except AgentMaintenanceActiveError as exc:
        raise RuntimeUnavailableError(
            "Agent version maintenance or publish activation is in progress; retry after it completes.",
        ) from exc
    return binding


def _validate_active_receipt_run(
    run: AgentRunModel,
    receipt: RuntimeReceipt,
) -> None:
    if run.trace_id != receipt.trace_id:
        raise RuntimeStateConflict("A run cannot be rebound to another trace")
    if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
        raise RuntimeStateConflict("Terminal run cannot accept new lifecycle events")


def _validate_run_input(input_value: Any) -> None:
    try:
        validate_no_permission_rules(input_value)
    except ValueError as exc:
        raise RuntimeInputRejected(str(exc)) from exc


def _recovery_blocks_terminal(run: AgentRunModel) -> bool:
    metadata = run.metadata_json or {}
    return metadata.get("recovery_required") is True or metadata.get("cancellation_requested") is True
