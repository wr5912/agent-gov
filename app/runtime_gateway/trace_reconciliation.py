"""后台用 Langfuse observation 对账终态 AgentGov runs。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.runtime.json_types import JsonObject

from .contracts import AgentRunResponse
from .store import RuntimeRunStore
from .trace_validation import trace_has_complete_governed_run

TRACE_RECONCILIATION_DEADLINE_SECONDS = 60


@dataclass
class RuntimeTraceReconciliationReport:
    scanned: int = 0
    completed: int = 0
    incomplete: int = 0
    pending: int = 0
    failures: int = 0


def reconcile_pending_traces(
    *,
    store: RuntimeRunStore,
    trace_fetcher: Callable[[str], JsonObject | None],
    now: datetime | None = None,
    deadline_seconds: int = TRACE_RECONCILIATION_DEADLINE_SECONDS,
    limit: int = 100,
) -> RuntimeTraceReconciliationReport:
    """逐 run 隔离查询失败；只有 durable facts 完整对账才标记 complete。"""

    if deadline_seconds <= 0:
        raise ValueError("Trace reconciliation deadline must be positive")
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Trace reconciliation now must be timezone-aware")
    report = RuntimeTraceReconciliationReport()
    for run in store.pending_terminal_traces(limit=limit):
        report.scanned += 1
        try:
            trace_id = run.trace_id
            if trace_id is None:
                report.pending += 1
                continue
            trace = trace_fetcher(trace_id)
            if trace is not None and trace.get("fetch_status") == "failed":
                report.failures += 1
                report.pending += 1
                continue
            if trace is not None and _trace_is_complete(store, run, trace):
                trace_url = trace.get("url")
                store.mark_trace_observed(
                    run.run_id,
                    trace_url=trace_url if isinstance(trace_url, str) else None,
                )
                report.completed += 1
            elif _deadline_elapsed(run, observed_at, deadline_seconds):
                updated = store.mark_trace_incomplete(run.run_id)
                if updated.trace_status == "incomplete":
                    report.incomplete += 1
                elif updated.trace_status == "complete":
                    report.completed += 1
                else:
                    report.pending += 1
            else:
                report.pending += 1
        except Exception:  # noqa: BLE001 - 单个坏 trace 不得阻断后续 runs。
            report.failures += 1
            report.pending += 1
    return report


def _trace_is_complete(store: RuntimeRunStore, run: AgentRunResponse, trace: JsonObject) -> bool:
    expectations = store.trace_expectations(run.run_id)
    return trace_has_complete_governed_run(trace, run, expectations)


def _deadline_elapsed(run: AgentRunResponse, now: datetime, deadline_seconds: int) -> bool:
    if not run.completed_at:
        return True
    try:
        completed_at = datetime.fromisoformat(run.completed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        return True
    return now.astimezone(timezone.utc) >= completed_at.astimezone(timezone.utc) + timedelta(seconds=deadline_seconds)
