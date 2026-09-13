from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from ._store_support import (
    RuntimeInputRejected,
    RuntimeStateConflict,
    _error_payload,
    _finish_run,
    _require_run,
    _run_response,
    _transition,
)
from .contracts import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AgentRunResponse,
    RunStatus,
    RuntimeReceipt,
)
from .models import AgentRunModel, RuntimeSessionBindingModel


class RuntimeRunRecoveryStoreMixin:
    """Run cancel/restart recovery 的单一事务边界。"""

    Session: sessionmaker

    def mark_cancel_requested(self, run_id: str) -> AgentRunResponse:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            metadata = dict(run.metadata_json or {})
            if metadata.get("cancellation_requested") is True:
                return _run_response(run)
            binding = db.get(RuntimeSessionBindingModel, run.session_id)
            if binding is None or binding.active_run_id != run.run_id or RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                raise RuntimeStateConflict("Only the Session's exact active run can be cancelled")
            metadata.update(
                {
                    "cancellation_requested": True,
                    "recovery_required": True,
                    "recovery_quiescent_observations": 0,
                },
            )
            metadata.pop("recovery_interrupt_requested", None)
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

    def recovery_required_runs(self) -> list[AgentRunResponse]:
        """返回持有恢复或取消 fence、必须先让上游静止的 runs。"""

        with self.Session() as db:
            rows = db.scalars(
                select(AgentRunModel)
                .where(
                    AgentRunModel.status.in_(
                        [item.value for item in ACTIVE_RUN_STATUSES],
                    ),
                )
                .order_by(AgentRunModel.updated_at),
            ).all()
            return [
                _run_response(row)
                for row in rows
                if (row.metadata_json or {}).get("recovery_required") is True or (row.metadata_json or {}).get("cancellation_requested") is True
            ]

    def mark_recovery_interrupt_requested(self, run_id: str) -> AgentRunResponse:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                return _run_response(run)
            metadata = dict(run.metadata_json or {})
            if metadata.get("recovery_interrupt_requested") is not True:
                metadata["recovery_interrupt_requested"] = True
                metadata.setdefault("recovery_quiescent_observations", 0)
                run.metadata_json = metadata
                run.updated_at = utc_now()
            return _run_response(run)

    def note_recovery_quiescent(self, run_id: str) -> int:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                return 0
            metadata = dict(run.metadata_json or {})
            observations = int(metadata.get("recovery_quiescent_observations") or 0) + 1
            metadata["recovery_quiescent_observations"] = observations
            run.metadata_json = metadata
            run.updated_at = utc_now()
            return observations

    def settle_after_quiescent_observation(
        self,
        run_id: str,
        *,
        error: str,
    ) -> AgentRunResponse | None:
        """原子记录一次静止观测，并仅在连续第二次时终态化。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                return _run_response(run)
            metadata = dict(run.metadata_json or {})
            observations = int(metadata.get("recovery_quiescent_observations") or 0) + 1
            metadata["recovery_quiescent_observations"] = observations
            run.metadata_json = metadata
            run.updated_at = utc_now()
            if observations < 2:
                return None
            _finalize_recovery(db, run, error=error)
            return _run_response(run)

    def reset_recovery_quiescent(self, run_id: str) -> AgentRunResponse:
        """任一 Team Session 非 idle 时清零连续静止观测。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                return _run_response(run)
            metadata = dict(run.metadata_json or {})
            if metadata.get("recovery_quiescent_observations") != 0:
                metadata["recovery_quiescent_observations"] = 0
                run.metadata_json = metadata
                run.updated_at = utc_now()
            return _run_response(run)

    def fail_recovery(self, run_id: str, *, error: str) -> AgentRunResponse:
        """上游确认静止后，按 canonical interruption 是否存在收敛并释放 fences。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, run_id)
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                return _run_response(run)
            _finalize_recovery(db, run, error=error)
            return _run_response(run)

    def reconcile_after_restart(self) -> list[str]:
        """进程重启后保留 active fence，等待 Runtime canonical receipt/reconcile。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            runs = list(
                db.scalars(
                    select(AgentRunModel).where(
                        AgentRunModel.status.in_(
                            [item.value for item in ACTIVE_RUN_STATUSES],
                        ),
                    ),
                ).all(),
            )
            for run in runs:
                metadata = dict(run.metadata_json or {})
                metadata.update(
                    {
                        "recovery_required": True,
                        "recovery_quiescent_observations": 0,
                    },
                )
                metadata.pop("recovery_interrupt_requested", None)
                run.metadata_json = metadata
                run.trace_status = "incomplete"
                run.updated_at = utc_now()
            return [run.run_id for run in runs]

    def reconcile_after_runtime_boot(self, boot_id: str, runtime_version: str) -> list[str]:
        """Runtime 单独重启时幂等保留 active fences 并强制 canonical recovery。"""

        if not boot_id or not runtime_version:
            raise RuntimeInputRejected("Runtime boot identity must not be empty")
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            runs = list(
                db.scalars(
                    select(AgentRunModel).where(
                        AgentRunModel.status.in_(
                            [item.value for item in ACTIVE_RUN_STATUSES],
                        ),
                    ),
                ).all(),
            )
            changed: list[str] = []
            for run in runs:
                metadata = dict(run.metadata_json or {})
                if metadata.get("runtime_boot_id") == boot_id:
                    if metadata.get("runtime_boot_version") != runtime_version:
                        raise RuntimeStateConflict("Runtime boot_id cannot be rebound to another version")
                    continue
                metadata.update(
                    {
                        "runtime_boot_id": boot_id,
                        "runtime_boot_version": runtime_version,
                        "recovery_required": True,
                        "recovery_quiescent_observations": 0,
                    },
                )
                metadata.pop("recovery_interrupt_requested", None)
                run.metadata_json = metadata
                run.trace_status = "incomplete"
                run.updated_at = utc_now()
                changed.append(run.run_id)
            return changed


def apply_runtime_interrupted_receipt(
    *,
    run: AgentRunModel,
    binding: RuntimeSessionBindingModel | None,
    receipt: RuntimeReceipt,
) -> None:
    """记录不带业务正文的中断证据；终态必须等待连续真实静止观测。"""

    validate_runtime_interrupted_receipt(run=run, binding=binding, receipt=receipt)
    current = RunStatus(run.status)
    if current in TERMINAL_RUN_STATUSES and current not in {
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }:
        raise RuntimeStateConflict("Completed run cannot accept RUN_INTERRUPTED")
    if current in ACTIVE_RUN_STATUSES and (binding is None or binding.active_run_id != run.run_id):
        raise RuntimeStateConflict("RUN_INTERRUPTED Session fence does not belong to the exact run")
    metadata = dict(run.metadata_json or {})
    interrupted_sessions = _validated_interrupted_session_ids(metadata)
    first_recovery_request = not interrupted_sessions
    if receipt.session_id not in interrupted_sessions:
        interrupted_sessions = [*interrupted_sessions, receipt.session_id]
    metadata.update(
        {
            "recovery_required": True,
            "runtime_interrupted_session_ids": interrupted_sessions,
        },
    )
    if first_recovery_request:
        metadata["recovery_quiescent_observations"] = 0
        metadata.pop("recovery_interrupt_requested", None)
    run.metadata_json = metadata
    run.trace_status = "pending"
    if current in TERMINAL_RUN_STATUSES:
        run.terminal_reason = "interrupted"
        run.error_json = {"type": "runtime_interrupted"}
    run.updated_at = utc_now()


def _validated_interrupted_session_ids(metadata: dict[str, object]) -> list[str]:
    raw = metadata.get("runtime_interrupted_session_ids")
    if not isinstance(raw, list):
        return []
    values = [value for value in raw if isinstance(value, str) and value]
    if len(values) != len(raw) or len(set(values)) != len(values):
        return []
    return values


def _finalize_recovery(
    db: Session,
    run: AgentRunModel,
    *,
    error: str,
) -> None:
    metadata = dict(run.metadata_json or {})
    if _validated_interrupted_session_ids(metadata):
        run.trace_status = "pending"
        run.terminal_reason = "interrupted"
        run.error_json = {"type": "runtime_interrupted"}
    else:
        run.trace_status = "incomplete"
        run.terminal_reason = "observation_incomplete"
        run.error_json = {"type": "runtime_recovery", "message": error}
    terminal = RunStatus.CANCELLED if metadata.get("cancellation_requested") is True else RunStatus.INTERRUPTED
    _transition(run, terminal)
    _finish_run(db, run)


def validate_runtime_interrupted_receipt(
    *,
    run: AgentRunModel,
    binding: RuntimeSessionBindingModel | None,
    receipt: RuntimeReceipt,
) -> None:
    if receipt.run_id != run.run_id:
        raise RuntimeStateConflict("RUN_INTERRUPTED must identify the exact run")
    if receipt.reply_id is not None or receipt.payload:
        raise RuntimeStateConflict("RUN_INTERRUPTED must not carry reply or content payload")
    if receipt.trace_id != run.trace_id:
        raise RuntimeStateConflict("A run cannot be rebound to another trace")
    if binding is None and receipt.session_id != run.session_id:
        raise RuntimeStateConflict("RUN_INTERRUPTED worker Session binding no longer exists")
    if binding is not None and binding.root_session_id != run.session_id:
        raise RuntimeStateConflict("RUN_INTERRUPTED Session is not bound to the run root")
