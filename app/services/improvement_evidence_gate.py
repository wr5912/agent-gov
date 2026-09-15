from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.runtime.errors import ConflictError
from app.runtime.json_types import JsonObject

FindRunById = Callable[[str], JsonObject | None]


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


@dataclass(frozen=True)
class ValidatedRunEvidence:
    run_id: str
    agent_id: str
    message: str
    answer_summary: str
    status: str
    trace_status: str
    trace_id: str


@dataclass(frozen=True)
class ValidatedRunEvidenceSnapshot:
    runs: tuple[ValidatedRunEvidence, ...]

    def get(self, run_id: str) -> ValidatedRunEvidence | None:
        return next((run for run in self.runs if run.run_id == run_id), None)


def require_automatic_feedback_evidence(
    feedbacks: list[Any],
    *,
    expected_agent_id: str,
    find_run_by_id: FindRunById | None,
) -> ValidatedRunEvidenceSnapshot:
    """Resolve one immutable, same-Agent snapshot for automatic improvement work."""

    if not expected_agent_id:
        raise ConflictError("Automatic improvement analysis requires an owning business agent")
    for feedback in feedbacks:
        if _text(getattr(feedback, "agent_id", "")) != expected_agent_id:
            raise ConflictError("Automatic improvement feedback belongs to a different business agent")
    if find_run_by_id is None:
        return ValidatedRunEvidenceSnapshot(())
    if not feedbacks:
        raise ConflictError("Automatic improvement analysis requires source feedback bound to a completed run")
    runs_by_id: dict[str, ValidatedRunEvidence] = {}
    for feedback in feedbacks:
        run_id = _text(getattr(feedback, "run_id", ""))
        if not run_id:
            raise ConflictError("Automatic improvement analysis requires every feedback item to carry run_id")
        if run_id not in runs_by_id:
            runs_by_id[run_id] = _resolve_run_evidence(
                run_id,
                expected_agent_id=expected_agent_id,
                find_run_by_id=find_run_by_id,
            )
    return ValidatedRunEvidenceSnapshot(tuple(runs_by_id.values()))


def _resolve_run_evidence(
    run_id: str,
    *,
    expected_agent_id: str,
    find_run_by_id: FindRunById,
) -> ValidatedRunEvidence:
    try:
        resolved_run = find_run_by_id(run_id)
    except Exception as exc:  # noqa: BLE001 - evidence lookup failures must fail closed
        raise ConflictError(f"Automatic improvement run evidence is unavailable: {run_id}") from exc
    if not resolved_run:
        raise ConflictError(f"Automatic improvement run evidence was not found: {run_id}")
    evidence = ValidatedRunEvidence(
        run_id=_text(resolved_run.get("run_id")),
        agent_id=_text(resolved_run.get("agent_id")),
        message=_text(resolved_run.get("message")),
        answer_summary=_text(resolved_run.get("answer_summary")),
        status=_text(resolved_run.get("status")),
        trace_status=_text(resolved_run.get("trace_status")),
        trace_id=_text(resolved_run.get("trace_id")),
    )
    if evidence.run_id != run_id:
        raise ConflictError(f"Automatic improvement run lookup returned a different run: {run_id}")
    if evidence.agent_id != expected_agent_id:
        raise ConflictError(f"Automatic improvement run belongs to a different business agent: {run_id}")
    if evidence.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
        raise ConflictError(f"Automatic improvement requires a terminal run: {run_id}")
    if evidence.trace_status != "complete" or not evidence.trace_id:
        raise ConflictError(f"Automatic improvement requires a complete Langfuse trace: {run_id}")
    return evidence
