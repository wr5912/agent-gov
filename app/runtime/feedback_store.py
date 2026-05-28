from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import yaml
from sqlalchemy import delete, or_, select

from .agent_version_store import SNAPSHOT_POLICY_VERSION, WORKSPACE_EXCLUDED_NAMES, WORKSPACE_EXCLUDED_PATTERNS
from .feedback_schemas import (
    validate_attribution_output,
    validate_execution_plan_output,
    validate_feedback_optimization_plan_output,
    validate_proposal_output,
)
from .runtime_db import (
    AgentRunModel,
    EvidenceFileModel,
    EvidencePackageModel,
    EvalCaseModel,
    EvalRunItemModel,
    EvalRunModel,
    ExternalGovernanceItemModel,
    ExternalNotificationModel,
    FeedbackCaseModel,
    FeedbackOptimizationBatchModel,
    FeedbackJobModel,
    FeedbackSignalModel,
    FeedbackSourceAnnotationModel,
    OptimizationProposalModel,
    OptimizationExecutionModel,
    OptimizationTaskModel,
    PendingCorrelationModel,
    ProposalReviewModel,
    SocEventModel,
    make_session_factory,
    runtime_db_path_from_data_dir,
    utc_now,
)
from .schemas import FeedbackSignalCreateRequest, SocEventIngestRequest


SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "header",
    "mcp_header",
    "password",
    "secret",
    "token",
)

MAX_EXECUTION_TARGET_CONTEXT_BYTES = 200_000


class FeedbackStore:
    """SQLAlchemy-backed store for the feedback optimization loop."""

    def __init__(
        self,
        *,
        data_dir: Path,
        workspace_dir: Optional[Path] = None,
        agent_version_provider: Optional[Callable[[], Optional[str]]] = None,
        runtime_version: str = "0.2.5",
        enable_debug_evidence: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.main_workspace_dir = workspace_dir or data_dir.parent / "main-workspace"
        self.db_path = runtime_db_path_from_data_dir(data_dir)
        self.Session = make_session_factory(self.db_path)
        self.agent_version_provider = agent_version_provider
        self.runtime_version = runtime_version
        self.enable_debug_evidence = enable_debug_evidence
        self.langfuse_trace_fetcher: Optional[Callable[[str], Optional[dict[str, Any]]]] = None
        self.tmp_jobs_dir = data_dir / ".runtime-tmp" / "jobs"
        self.tmp_jobs_dir.mkdir(parents=True, exist_ok=True)

        # Compatibility-only paths. They are not authoritative and are not created.
        self.runs_dir = data_dir / "agent-runs"
        self.signal_dir = data_dir / "feedback-signals"
        self.event_dir = data_dir / "soc-events"
        self.pending_dir = data_dir / "pending-correlations"
        self.case_dir = data_dir / "feedback-cases"
        self.evidence_dir = data_dir / "evidence-packages"
        self.jobs_dir = data_dir / "feedback-analysis" / "jobs"
        self.proposal_dir = data_dir / "optimization-proposals"
        self.task_dir = data_dir / "optimization-tasks"
        self.external_webhooks_path = data_dir / "external-governance-webhooks.yaml"

    def set_langfuse_trace_fetcher(self, fetcher: Callable[[str], Optional[dict[str, Any]]]) -> None:
        # Trace details are intentionally not persisted in SQLite; keep the setter
        # for runtime wiring compatibility and possible live trace lookups.
        self.langfuse_trace_fetcher = fetcher

    @property
    def runs_path(self) -> Path:
        return self.runs_dir / "runs.jsonl"

    @property
    def signals_path(self) -> Path:
        return self.signal_dir / "signals.jsonl"

    @property
    def events_path(self) -> Path:
        return self.event_dir / "events.jsonl"

    @property
    def pending_path(self) -> Path:
        return self.pending_dir / "pending.jsonl"

    @property
    def cases_path(self) -> Path:
        return self.case_dir / "cases.jsonl"

    @property
    def jobs_path(self) -> Path:
        return self.jobs_dir / "jobs.jsonl"

    @property
    def proposals_path(self) -> Path:
        return self.proposal_dir / "proposals.jsonl"

    @property
    def proposal_reviews_path(self) -> Path:
        return self.proposal_dir / "reviews.jsonl"

    @property
    def tasks_path(self) -> Path:
        return self.task_dir / "tasks.jsonl"

    def record_run(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = record if self.enable_debug_evidence else self._scrub_record(record)
        run_id = self._string(payload.get("run_id")) or f"run-{uuid.uuid4()}"
        payload = {**payload, "run_id": run_id, "created_at": payload.get("created_at") or utc_now()}
        with self.Session.begin() as db:
            existing = db.get(AgentRunModel, run_id)
            values = {
                "session_id": self._string(payload.get("session_id")),
                "sdk_session_id": self._string(payload.get("sdk_session_id")),
                "agent_version_id": self._string(payload.get("agent_version_id")),
                "alert_id": self._string(payload.get("alert_id")),
                "case_id": self._string(payload.get("case_id")),
                "created_at": str(payload.get("created_at")),
                "completed_at": self._string(payload.get("completed_at")),
                "langfuse_trace_id": self._string(payload.get("langfuse_trace_id")),
                "langfuse_trace_url": self._string(payload.get("langfuse_trace_url")),
                "payload_json": payload,
            }
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                db.add(AgentRunModel(run_id=run_id, **values))
        return payload

    def list_runs(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.Session() as db:
            records = [row.payload_json for row in db.scalars(select(AgentRunModel).order_by(AgentRunModel.created_at.desc())).all()]
        return self._filter_records(records, {"run_id": run_id, "session_id": session_id, "alert_id": alert_id, "case_id": case_id}, limit)

    def create_signal(self, req: FeedbackSignalCreateRequest) -> dict[str, Any]:
        payload = self._scrub_record(req.model_dump(mode="json"))
        if payload.get("source_type") == "implicit_feedback":
            payload["auto_captured"] = True
            payload["requires_review"] = True
        if not payload.get("run_id") and not (payload.get("session_id") or payload.get("alert_id") or payload.get("case_id")):
            raise ValueError("Feedback signal requires run_id, session_id, alert_id, or case_id")

        run = self.find_run(run_id=self._string(payload.get("run_id")))
        if run:
            payload["session_id"] = payload.get("session_id") or run.get("session_id")
            payload["alert_id"] = payload.get("alert_id") or run.get("alert_id")
            payload["case_id"] = payload.get("case_id") or run.get("case_id")
        signal = {
            **payload,
            "signal_id": payload.get("signal_id") or f"fbs-{uuid.uuid4()}",
            "created_at": utc_now(),
            "matched_run_id": run.get("run_id") if run else None,
        }
        with self.Session.begin() as db:
            db.merge(
                FeedbackSignalModel(
                    signal_id=signal["signal_id"],
                    source_type=signal.get("source_type") or "explicit_feedback",
                    run_id=self._string(signal.get("run_id")),
                    matched_run_id=self._string(signal.get("matched_run_id")),
                    session_id=self._string(signal.get("session_id")),
                    alert_id=self._string(signal.get("alert_id")),
                    case_id=self._string(signal.get("case_id")),
                    created_at=signal["created_at"],
                    payload_json=signal,
                )
            )
        return signal

    def list_signals(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {
            "run_id": run_id,
            "matched_run_id": run_id,
            "session_id": session_id,
            "alert_id": alert_id,
            "case_id": case_id,
            "source_type": source_type,
        }
        with self.Session() as db:
            records = [row.payload_json for row in db.scalars(select(FeedbackSignalModel).order_by(FeedbackSignalModel.created_at.desc())).all()]
        return self._filter_records(records, filters, limit, any_key_groups=[("run_id", "matched_run_id")])

    def find_signal(self, signal_id: str) -> Optional[dict[str, Any]]:
        if not signal_id:
            return None
        with self.Session() as db:
            record = db.get(FeedbackSignalModel, signal_id)
            return record.payload_json if record else None

    def ingest_soc_event(self, req: SocEventIngestRequest) -> dict[str, Any]:
        existing = self.find_event(req.event_id)
        if existing:
            return {
                "event": existing,
                "correlation_status": "duplicate",
                "matched_run_id": existing.get("matched_run_id"),
                "pending_correlation": None,
            }

        payload = self._scrub_record(req.model_dump(mode="json"))
        payload["auto_captured"] = True
        payload["requires_review"] = True if payload.get("requires_review") is None else payload.get("requires_review")
        run = self.find_run_for_event(payload)
        event = {
            "created_at": utc_now(),
            **payload,
            "matched_run_id": run.get("run_id") if run else None,
        }

        pending = None
        status = "matched"
        with self.Session.begin() as db:
            db.add(
                SocEventModel(
                    event_id=event["event_id"],
                    event_type=event["event_type"],
                    source_system=event["source_system"],
                    run_id=self._string(event.get("run_id")),
                    matched_run_id=self._string(event.get("matched_run_id")),
                    session_id=self._string(event.get("session_id")),
                    alert_id=self._string(event.get("alert_id")),
                    case_id=self._string(event.get("case_id")),
                    created_at=event["created_at"],
                    payload_json=event,
                )
            )
            if not run:
                status = "pending_correlation"
                pending = {
                    "pending_id": f"pc-{uuid.uuid4()}",
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "status": "pending",
                    "reason": "no_matching_run",
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "source_system": event["source_system"],
                    "session_id": event.get("session_id"),
                    "alert_id": event.get("alert_id"),
                    "case_id": event.get("case_id"),
                }
                db.add(
                    PendingCorrelationModel(
                        pending_id=pending["pending_id"],
                        event_id=pending["event_id"],
                        status=pending["status"],
                        created_at=pending["created_at"],
                        updated_at=pending["updated_at"],
                        payload_json=pending,
                    )
                )

        return {
            "event": event,
            "correlation_status": status,
            "matched_run_id": event.get("matched_run_id"),
            "pending_correlation": pending,
        }

    def list_events(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {
            "run_id": run_id,
            "matched_run_id": run_id,
            "session_id": session_id,
            "alert_id": alert_id,
            "case_id": case_id,
            "event_type": event_type,
        }
        with self.Session() as db:
            records = [row.payload_json for row in db.scalars(select(SocEventModel).order_by(SocEventModel.created_at.desc())).all()]
        return self._filter_records(records, filters, limit, any_key_groups=[("run_id", "matched_run_id")])

    def find_event(self, event_id: str) -> Optional[dict[str, Any]]:
        if not event_id:
            return None
        with self.Session() as db:
            record = db.get(SocEventModel, event_id)
            return record.payload_json if record else None

    def list_pending(self, *, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as db:
            records = [row.payload_json for row in db.scalars(select(PendingCorrelationModel).order_by(PendingCorrelationModel.updated_at.desc())).all()]
        return self._filter_records(records, {"status": status}, limit)

    def find_pending(self, pending_id: str) -> Optional[dict[str, Any]]:
        if not pending_id:
            return None
        with self.Session() as db:
            record = db.get(PendingCorrelationModel, pending_id)
            if not record:
                record = db.scalar(select(PendingCorrelationModel).where(PendingCorrelationModel.event_id == pending_id))
            return record.payload_json if record else None

    def resolve_pending(
        self,
        pending_id: str,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        pending = self.find_pending(pending_id)
        if not pending:
            return None
        resolved = {
            **pending,
            "updated_at": utc_now(),
            "status": "resolved",
            "resolved_run_id": run_id,
            "session_id": session_id or pending.get("session_id"),
            "alert_id": alert_id or pending.get("alert_id"),
            "case_id": case_id or pending.get("case_id"),
            "comment": comment,
        }
        with self.Session.begin() as db:
            record = db.get(PendingCorrelationModel, pending["pending_id"])
            if record:
                record.status = "resolved"
                record.updated_at = resolved["updated_at"]
            record.payload_json = resolved
        return resolved

    def list_feedback_sources(self, *, limit: int = 500) -> list[dict[str, Any]]:
        annotations = self._source_annotations_by_key()
        cases_by_source_id = self._cases_by_source_id()
        rows: list[dict[str, Any]] = []
        rows.extend(
            self._source_row(
                source_kind="signal",
                source_id=str(item["signal_id"]),
                raw=item,
                annotation=annotations.get(("signal", str(item["signal_id"]))),
                feedback_case=cases_by_source_id.get(str(item["signal_id"])),
            )
            for item in self.list_signals(limit=limit)
        )
        rows.extend(
            self._source_row(
                source_kind="soc_event",
                source_id=str(item["event_id"]),
                raw=item,
                annotation=annotations.get(("soc_event", str(item["event_id"]))),
                feedback_case=cases_by_source_id.get(str(item["event_id"])),
            )
            for item in self.list_events(limit=limit)
        )
        rows.extend(
            self._source_row(
                source_kind="pending_correlation",
                source_id=str(item["pending_id"]),
                raw=item,
                annotation=annotations.get(("pending_correlation", str(item["pending_id"]))),
                feedback_case=cases_by_source_id.get(str(item["pending_id"])),
            )
            for item in self.list_pending(limit=limit)
        )
        rows.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
        return rows[:limit]

    def find_feedback_source(self, source_kind: str, source_id: str) -> Optional[dict[str, Any]]:
        kind = self._normalize_source_kind(source_kind)
        raw = self._find_source_record(kind, source_id)
        if not raw:
            return None
        annotation = self._find_source_annotation(kind, source_id)
        feedback_case = self._find_case_for_source_id(source_id)
        return self._source_row(
            source_kind=kind,
            source_id=source_id,
            raw=raw,
            annotation=annotation,
            feedback_case=feedback_case,
        )

    def update_feedback_source_annotation(self, source_kind: str, source_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        kind = self._normalize_source_kind(source_kind)
        raw = self._find_source_record(kind, source_id)
        if not raw:
            return None
        annotation_id = self._source_annotation_id(kind, source_id)
        now = utc_now()
        with self.Session.begin() as db:
            row = db.get(FeedbackSourceAnnotationModel, annotation_id)
            payload = dict(row.payload_json or {}) if row else {}
            created_at = row.created_at if row else now
            payload.update(
                {
                    "annotation_id": annotation_id,
                    "source_kind": kind,
                    "source_id": source_id,
                    "created_at": created_at,
                    "updated_at": now,
                }
            )
            for key in ("comment", "labels", "priority", "status", "requires_review", "metadata"):
                if key in fields:
                    value = fields.get(key)
                    if key == "labels" and value is not None:
                        payload[key] = self._unique_strings([str(item).strip() for item in value if str(item).strip()])
                    elif key == "metadata" and value is not None:
                        payload[key] = dict(value)
                    else:
                        payload[key] = value
            payload["status"] = self._string(payload.get("status")) or "triaged"
            if row:
                row.status = str(payload["status"])
                row.updated_at = now
                row.payload_json = payload
            else:
                db.add(
                    FeedbackSourceAnnotationModel(
                        annotation_id=annotation_id,
                        source_kind=kind,
                        source_id=source_id,
                        status=str(payload["status"]),
                        created_at=created_at,
                        updated_at=now,
                        payload_json=payload,
                    )
                )
        return self.find_feedback_source(kind, source_id)

    def ensure_case_for_source(self, source_kind: str, source_id: str, *, priority: str = "medium") -> Optional[dict[str, Any]]:
        kind = self._normalize_source_kind(source_kind)
        if not self._find_source_record(kind, source_id):
            return None
        existing = self._find_case_for_source_id(source_id)
        if existing:
            return existing
        source = self.find_feedback_source(kind, source_id)
        annotation_priority = self._string((source or {}).get("priority"))
        created = self.create_case(
            source_ids=[source_id],
            title=self._source_case_title(source or {}),
            priority=annotation_priority or priority or "medium",
        )
        return created

    def generate_eval_cases_for_sources(self, source_refs: list[dict[str, Any]], *, force: bool = False) -> dict[str, Any]:
        created = 0
        reused = 0
        updated = 0
        skipped = 0
        eval_cases: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for ref in self._normalize_source_refs(source_refs):
            feedback_case = self.ensure_case_for_source(ref["source_kind"], ref["source_id"])
            if not feedback_case:
                skipped += 1
                results.append({**ref, "status": "skipped", "reason": "source cannot create feedback case"})
                continue
            existing = self.find_eval_case(source_feedback_case_id=feedback_case["feedback_case_id"])
            payload = self._build_eval_case_from_source(ref, feedback_case)
            if not payload:
                skipped += 1
                results.append({**ref, "feedback_case_id": feedback_case["feedback_case_id"], "status": "skipped", "reason": "missing prompt"})
                continue
            if existing and not force:
                reused += 1
                eval_cases.append(existing)
                results.append({**ref, "feedback_case_id": feedback_case["feedback_case_id"], "eval_case_id": existing["eval_case_id"], "status": "reused"})
                continue
            if existing and force:
                payload["eval_case_id"] = existing["eval_case_id"]
                payload["created_at"] = existing["created_at"]
                self._replace_eval_case_payload(payload)
                refreshed = self.find_eval_case(existing["eval_case_id"])
                if refreshed:
                    eval_cases.append(refreshed)
                updated += 1
                results.append({**ref, "feedback_case_id": feedback_case["feedback_case_id"], "eval_case_id": existing["eval_case_id"], "status": "updated"})
                continue
            with self.Session.begin() as db:
                db.add(
                    EvalCaseModel(
                        eval_case_id=payload["eval_case_id"],
                        created_at=payload["created_at"],
                        updated_at=payload["updated_at"],
                        status=payload["status"],
                        source_feedback_case_id=self._string(payload.get("source_feedback_case_id")),
                        source_run_id=self._string(payload.get("source_run_id")),
                        labels_json=list(payload.get("labels") or []),
                        payload_json=payload,
                    )
                )
            created += 1
            eval_cases.append(payload)
            results.append({**ref, "feedback_case_id": feedback_case["feedback_case_id"], "eval_case_id": payload["eval_case_id"], "status": "created"})
        return {"created": created, "reused": reused, "updated": updated, "skipped": skipped, "eval_cases": eval_cases, "results": results}

    def create_optimization_batch(
        self,
        source_refs: list[dict[str, Any]],
        *,
        title: Optional[str] = None,
        priority: str = "medium",
    ) -> Optional[dict[str, Any]]:
        refs = self._normalize_source_refs(source_refs)
        if not refs:
            return None
        feedback_cases: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for ref in refs:
            feedback_case = self.ensure_case_for_source(ref["source_kind"], ref["source_id"], priority=priority)
            if feedback_case:
                feedback_cases.append(feedback_case)
                self.update_feedback_source_annotation(ref["source_kind"], ref["source_id"], {"status": "in_batch", "priority": priority})
            else:
                skipped.append({**ref, "reason": "source cannot create feedback case"})
        if not feedback_cases:
            return None
        eval_result = self.generate_eval_cases_for_sources(refs, force=False)
        now = utc_now()
        feedback_case_ids = self._unique_strings([item.get("feedback_case_id") for item in feedback_cases])
        eval_case_ids = self._unique_strings([item.get("eval_case_id") for item in eval_result.get("eval_cases") or []])
        batch_id = f"fob-{uuid.uuid4()}"
        payload = {
            "schema_version": "feedback-optimization-batch/v1",
            "batch_id": batch_id,
            "created_at": now,
            "updated_at": now,
            "status": "draft",
            "title": title or f"反馈优化批次 {len(feedback_case_ids)} 条反馈",
            "priority": priority or "medium",
            "source_refs": refs,
            "feedback_case_ids": feedback_case_ids,
            "skipped_source_refs": skipped,
            "eval_case_ids": eval_case_ids,
            "eval_case_generation": eval_result,
            "attribution_job_ids": [],
            "optimization_plan": None,
            "optimization_task_id": None,
            "execution_job_id": None,
            "eval_run_id": None,
        }
        with self.Session.begin() as db:
            db.add(
                FeedbackOptimizationBatchModel(
                    batch_id=batch_id,
                    created_at=now,
                    updated_at=now,
                    status="draft",
                    title=payload["title"],
                    payload_json=payload,
                )
            )
        return self.find_optimization_batch(batch_id)

    def list_optimization_batches(self, *, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as db:
            rows = db.scalars(select(FeedbackOptimizationBatchModel).order_by(FeedbackOptimizationBatchModel.updated_at.desc())).all()
            batches = [self._batch_to_dict(row) for row in rows]
        return self._filter_records(batches, {"status": status}, limit)

    def find_optimization_batch(self, batch_id: str) -> Optional[dict[str, Any]]:
        if not batch_id:
            return None
        with self.Session() as db:
            row = db.get(FeedbackOptimizationBatchModel, batch_id)
            return self._batch_to_dict(row) if row else None

    def record_batch_attribution_jobs(self, batch_id: str, jobs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        job_ids = self._unique_strings([job.get("job_id") for job in jobs])
        completed = [job for job in jobs if job.get("status") == "completed"]
        failed = [job for job in jobs if job.get("status") in {"failed", "needs_human_review", "timeout"}]
        running = [job for job in jobs if job.get("status") in {"created", "queued", "running", "schema_validating", "evidence_packaging"}]
        batch = self.find_optimization_batch(batch_id)
        expected_total = len((batch or {}).get("feedback_case_ids") or [])
        total = max(expected_total, len(jobs))
        if total and len(completed) == total:
            status = "attribution_completed"
        elif failed:
            status = "needs_human_review"
        else:
            status = "attribution_running"
        return self._update_batch(
            batch_id,
            status=status,
            fields={
                "attribution_job_ids": job_ids,
                "attribution_jobs": jobs,
                "attribution_summary": {
                    "total": total,
                    "completed": len(completed),
                    "running": len(running),
                    "needs_review_or_failed": len(failed),
                },
            },
        )

    def reset_batch_attribution(self, batch_id: str) -> Optional[dict[str, Any]]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        task_id = self._string(batch.get("optimization_task_id"))
        task = self.find_task(task_id) if task_id else None
        if task and task.get("applied_agent_version_id"):
            raise ValueError("当前批次已应用并产生 Agent 版本，不能原地重新归因；请基于反馈信息创建新批次。")
        for feedback_case_id in batch.get("feedback_case_ids") or []:
            self.discard_current_attribution(str(feedback_case_id), invalidate_downstream=True)
        self._discard_batch_draft_artifacts(batch)
        return self._update_batch(
            batch_id,
            status="draft",
            fields={
                "attribution_job_ids": [],
                "attribution_jobs": [],
                "attribution_summary": {"total": len(batch.get("feedback_case_ids") or []), "completed": 0, "running": 0, "needs_review_or_failed": 0},
                "optimization_plan": None,
                "internal_proposal_id": None,
                "optimization_task_id": None,
                "optimization_task": None,
                "execution_job_id": None,
                "execution_job": None,
                "eval_run_id": None,
                "latest_eval_run": None,
                "execution_apply_result": None,
            },
        )

    def generate_batch_optimization_plan(self, batch_id: str, *, regeneration_instruction: Optional[str] = None) -> Optional[dict[str, Any]]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        self._assert_batch_plan_can_regenerate(batch)
        instruction = regeneration_instruction.strip() if isinstance(regeneration_instruction, str) else None
        instruction = instruction or None
        attributions = self._batch_attribution_outputs(batch)
        if not attributions:
            return self._update_batch(
                batch_id,
                status="needs_human_review",
                fields={"optimization_plan": self._non_actionable_plan(batch, "暂无可用归因结果，不能生成可执行优化方案。", instruction)},
            )
        plan = self._build_batch_optimization_plan(batch, attributions, regeneration_instruction=instruction)
        return self._update_batch(
            batch_id,
            status=plan["status"],
            fields={"optimization_plan": plan, "updated_at": utc_now()},
        )

    def create_batch_plan_job(
        self,
        batch_id: str,
        *,
        profile_version: Optional[dict[str, Any]] = None,
        force: bool = True,
        regeneration_instruction: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        self._assert_batch_plan_can_regenerate(batch)
        instruction = regeneration_instruction.strip() if isinstance(regeneration_instruction, str) else None
        instruction = instruction or None
        if not force and not instruction and isinstance(batch.get("optimization_plan"), dict):
            return {"_reused_existing": True, **batch}
        attributions = self._batch_attribution_outputs(batch)
        if not attributions:
            self._update_batch(
                batch_id,
                status="needs_human_review",
                fields={
                    "optimization_plan": self._non_actionable_plan(batch, "暂无可用归因结果，不能生成可执行优化方案。", instruction),
                    "optimization_plan_job_id": None,
                    "optimization_plan_job": None,
                    "optimization_plan_error": None,
                },
            )
            return {"_no_actionable_attributions": True, "batch_id": batch_id}

        feedback_case_id = self._latest(batch.get("feedback_case_ids")) or self._string(attributions[0].get("feedback_case_id")) or ""
        feedback_case = self.find_case(feedback_case_id) if feedback_case_id else None
        evidence_package_id = self._latest((feedback_case or {}).get("evidence_package_ids")) or f"batch-evidence-{batch_id}"
        job_id = f"fbp-{uuid.uuid4()}"
        input_payload = {
            "schema_version": "feedback-optimization-plan-input/v1",
            "job_id": job_id,
            "batch_id": batch_id,
            "feedback_case_ids": batch.get("feedback_case_ids") or [],
            "eval_case_ids": batch.get("eval_case_ids") or [],
            "source_refs": batch.get("source_refs") or [],
            "attribution_job_ids": self._unique_strings([self._string(item.get("_job_id") or item.get("attribution_job_id")) or "" for item in attributions]),
            "attribution_outputs": attributions,
            "eval_cases": [case for case in (self.find_eval_case(str(eval_case_id)) for eval_case_id in batch.get("eval_case_ids") or []) if case],
            "main_agent_version_id": self._current_agent_version_id(),
            "main_agent_manifest_path": str(self.data_dir / "agent-versions" / "main" / "current.json"),
            "allowed_target_paths": ["<any-managed-main-workspace-relative-file>"],
            "target_policy": self._execution_target_policy(),
            "task": "generate_feedback_optimization_plan",
        }
        if instruction:
            input_payload["regeneration_instruction"] = instruction
        input_path = self._write_job_input(job_id, "batch_plan", input_payload)
        job = self._job_record(
            job_id=job_id,
            job_type="batch_plan",
            feedback_case_id=feedback_case_id,
            evidence_package_id=evidence_package_id,
            status="queued",
            profile_name="proposal-generator",
            input_path=input_path,
            profile_version=profile_version,
        )
        job["input_json"] = input_payload
        with self.Session.begin() as db:
            db.add(self._job_model_from_dict(job))
        self._update_batch(
            batch_id,
            status="optimization_plan_queued",
            fields={
                "optimization_plan_job_id": job_id,
                "optimization_plan_job": self.get_job(job_id),
                "optimization_plan_error": None,
            },
        )
        return self.get_job(job_id)

    def complete_batch_plan_job(self, job_id: str, raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return None
        batch_id = self._job_batch_id(job)
        self._set_job_json(job_id, raw_output_json=raw_output)
        self._append_job_update(job_id, status="schema_validating")
        validated, error = validate_feedback_optimization_plan_output(raw_output)
        if not validated:
            self._write_job_error(job, "SCHEMA_VALIDATION_FAILED", error or "invalid feedback optimization plan output")
            completed = self._append_job_update(job_id, status="needs_human_review", completed_at=utc_now())
            if batch_id:
                self._update_batch(
                    batch_id,
                    status="needs_human_review",
                    fields={
                        "optimization_plan_job_id": job_id,
                        "optimization_plan_job": completed,
                        "optimization_plan_error": (completed or {}).get("error_json"),
                    },
                )
            self._cleanup_job_tmp(job_id)
            return completed

        plan = self._normalize_batch_plan_output(validated, job)
        self._set_job_json(job_id, validated_output_json=validated)
        completed = self._append_job_update(job_id, status="completed", completed_at=utc_now())
        if batch_id:
            self._update_batch(
                batch_id,
                status=plan["status"],
                fields={
                    "optimization_plan": plan,
                    "optimization_plan_job_id": job_id,
                    "optimization_plan_job": completed,
                    "optimization_plan_error": None,
                },
            )
        self._cleanup_job_tmp(job_id)
        return completed

    def offline_batch_plan_output(self, job: dict[str, Any]) -> dict[str, Any]:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        batch_id = self._string(input_json.get("batch_id")) or ""
        return {
            "schema_version": "feedback-optimization-plan-output/v1",
            "batch_id": batch_id,
            "status": "needs_human_review",
            "title": "当前不能生成可执行优化方案",
            "summary": "当前未配置模型提供商，proposal-generator 无法生成批次优化任务。",
            "problem_types": [],
            "confidence": "low",
            "actionability": "needs_human_analysis",
            "target_type": "not_actionable",
            "target_path": None,
            "recommendation": "配置模型提供商后重新生成优化方案，或由开发人员手工分析归因结果。",
            "expected_effect": "离线占位不会改变主智能体行为。",
            "validation": "重新生成真实优化方案后，再使用本批次回归测试用例验证。",
            "risk": "离线占位没有可执行任务。",
            "source_refs": input_json.get("source_refs") or [],
            "feedback_case_ids": input_json.get("feedback_case_ids") or [],
            "eval_case_ids": input_json.get("eval_case_ids") or [],
            "attribution_job_ids": input_json.get("attribution_job_ids") or [],
            "attribution_summaries": [],
            "rationale": "未配置模型提供商，系统不能运行 proposal-generator。",
            "evidence_refs": [],
            "tasks": [],
            "blocked_items": [
                {
                    "title": "未配置模型提供商",
                    "target_type": "not_actionable",
                    "actionability": "needs_human_analysis",
                    "reason": "当前未配置模型提供商，不能由 proposal-generator 生成可执行优化任务。",
                    "feedback_case_ids": input_json.get("feedback_case_ids") or [],
                    "eval_case_ids": input_json.get("eval_case_ids") or [],
                    "attribution_job_ids": input_json.get("attribution_job_ids") or [],
                }
            ],
        }

    def approve_batch_optimization_plan(self, batch_id: str, *, comment: Optional[str] = None) -> Optional[dict[str, Any]]:
        batch = self.find_optimization_batch(batch_id)
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        if not batch or not plan or plan.get("status") != "pending_approval":
            return None
        target_path = self._string(plan.get("target_path"))
        if not target_path or not self._target_allowed(target_path):
            raise ValueError("Optimization plan target is not actionable")
        proposal_id = self._string(plan.get("internal_proposal_id")) or f"prop-{uuid.uuid4()}"
        feedback_case_id = self._latest(batch.get("feedback_case_ids")) or ""
        now = utc_now()
        proposal_actionability = self._string(plan.get("actionability"))
        if proposal_actionability not in {"direct_workspace_change", "workspace_config_change"}:
            proposal_actionability = "direct_workspace_change"
        proposal = {
            "proposal_id": proposal_id,
            "created_at": now,
            "feedback_case_id": feedback_case_id,
            "proposal_job_id": f"batch-plan-{batch_id}",
            "status": "approved",
            "actionability": plan.get("actionability") or "direct_workspace_change",
            "target_type": plan.get("target_type") or plan.get("optimization_object_type") or "main_agent_claude_md",
            "target_path": target_path,
            "title": plan.get("title") or "反馈批次优化方案",
            "recommendation": plan.get("recommendation") or "",
            "expected_effect": plan.get("expected_effect") or "",
            "validation": plan.get("validation") or "",
            "risk": plan.get("risk") or "",
            "regeneration_instruction": plan.get("regeneration_instruction"),
            "requires_approval": True,
            "base_agent_version_id": self._current_agent_version_id(),
            "source_batch_id": batch_id,
            "source_feedback_case_ids": batch.get("feedback_case_ids") or [],
            "source_refs": batch.get("source_refs") or [],
            "latest_review": {
                "review_id": f"opr-{uuid.uuid4()}",
                "proposal_id": proposal_id,
                "created_at": now,
                "action": "approve",
                "status": "approved",
                "comment": comment,
                "source": "feedback_optimization_batch",
            },
        }
        with self.Session.begin() as db:
            db.merge(self._proposal_model_from_dict(proposal))
        task = self.create_task(proposal_id=proposal_id, execution_mode="manual_or_patch", comment=comment or f"由优化批次 {batch_id} 执行创建。")
        if not task:
            return None
        task = self._update_task_payload(
            task["optimization_task_id"],
            status=task["status"],
            fields={
                "source": "feedback_optimization_batch",
                "source_batch_id": batch_id,
                "feedback_case_ids": batch.get("feedback_case_ids") or [],
                "eval_case_ids": batch.get("eval_case_ids") or [],
            },
        ) or task
        approved_plan = {**plan, "status": "approved", "approved_at": utc_now(), "approval_comment": comment, "internal_proposal_id": proposal_id}
        updated_batch = self._update_batch(
            batch_id,
            status="execution_planning",
            fields={
                "optimization_plan": approved_plan,
                "internal_proposal_id": proposal_id,
                "optimization_task_id": task["optimization_task_id"],
                "optimization_task": task,
            },
        )
        return {"batch": updated_batch, "optimization_task": task}

    def reject_batch_optimization_plan(self, batch_id: str, *, comment: Optional[str] = None) -> Optional[dict[str, Any]]:
        batch = self.find_optimization_batch(batch_id)
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        if not batch or not plan:
            return None
        rejected_plan = {**plan, "status": "rejected", "rejected_at": utc_now(), "rejection_comment": comment}
        return self._update_batch(batch_id, status="rejected", fields={"optimization_plan": rejected_plan})

    def prepare_batch_plan_task_execution(
        self,
        batch_id: str,
        plan_task_id: str,
        *,
        comment: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        batch, plan, plan_task = self._batch_plan_task(batch_id, plan_task_id)
        if not batch or not plan or not plan_task:
            return None
        if plan_task.get("execution_kind") != "workspace_execution":
            raise ValueError("Optimization plan task is not executable by execution-optimizer")
        target_path = self._string(plan_task.get("target_path"))
        if not target_path or not self._target_allowed(target_path):
            raise ValueError("Optimization plan task target is not actionable")
        existing_task_id = self._string(plan_task.get("optimization_task_id"))
        existing_task = self.find_task(existing_task_id) if existing_task_id else None
        if existing_task:
            updated = self._update_batch_plan_task(
                batch_id,
                plan_task_id,
                {
                    "status": existing_task.get("status") or "execution_planning",
                    "optimization_task_id": existing_task["optimization_task_id"],
                    "execution_job_id": existing_task.get("latest_execution_job_id"),
                    "latest_execution_job": existing_task.get("latest_execution_job"),
                    "applied_agent_version_id": existing_task.get("applied_agent_version_id"),
                },
                batch_status=str(existing_task.get("status") or "execution_planning"),
                top_level_fields={"optimization_task_id": existing_task["optimization_task_id"], "optimization_task": existing_task},
            )
            return {"batch": updated, "optimization_task": existing_task, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}

        proposal_id = self._string(plan_task.get("internal_proposal_id")) or f"prop-{uuid.uuid4()}"
        feedback_case_id = self._latest(plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids")) or ""
        now = utc_now()
        proposal_actionability = self._string(plan_task.get("actionability"))
        if proposal_actionability not in {"direct_workspace_change", "workspace_config_change"}:
            proposal_actionability = "direct_workspace_change"
        proposal = {
            "proposal_id": proposal_id,
            "created_at": now,
            "feedback_case_id": feedback_case_id,
            "proposal_job_id": f"batch-plan-task-{batch_id}-{plan_task_id}",
            "status": "approved",
            "actionability": proposal_actionability,
            "target_type": plan_task.get("target_type") or "main_agent_claude_md",
            "target_path": target_path,
            "title": plan_task.get("title") or plan.get("title") or "反馈批次优化任务",
            "description": plan_task.get("description") or "",
            "objective": plan_task.get("objective") or "",
            "target_summary": plan_task.get("target_summary") or "",
            "task_context": plan_task.get("task_context") if isinstance(plan_task.get("task_context"), dict) else {},
            "recommendation": plan_task.get("recommendation") or plan.get("recommendation") or "",
            "recommended_actions": plan_task.get("recommended_actions") or [],
            "acceptance_criteria": plan_task.get("acceptance_criteria") or [],
            "expected_effect": plan_task.get("expected_effect") or plan.get("expected_effect") or "",
            "validation": plan_task.get("validation") or plan.get("validation") or "",
            "risk": plan_task.get("risk") or plan.get("risk") or "",
            "analysis_summary": plan_task.get("analysis_summary") or "",
            "evidence_summary": plan_task.get("evidence_summary") or "",
            "evidence_refs": plan_task.get("evidence_refs") or [],
            "regeneration_instruction": plan.get("regeneration_instruction"),
            "requires_approval": True,
            "base_agent_version_id": self._current_agent_version_id(),
            "source_batch_id": batch_id,
            "source_plan_task_id": plan_task_id,
            "source_feedback_case_ids": plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
            "source_refs": batch.get("source_refs") or [],
            "latest_review": {
                "review_id": f"opr-{uuid.uuid4()}",
                "proposal_id": proposal_id,
                "created_at": now,
                "action": "approve",
                "status": "approved",
                "comment": comment,
                "source": "feedback_optimization_plan_task",
            },
        }
        with self.Session.begin() as db:
            db.merge(self._proposal_model_from_dict(proposal))
        task = self.create_task(proposal_id=proposal_id, execution_mode="manual_or_patch", comment=comment or f"由优化批次 {batch_id} 的任务 {plan_task_id} 执行创建。")
        if not task:
            return None
        task = self._update_task_payload(
            task["optimization_task_id"],
            status=task["status"],
            fields={
                "source": "feedback_optimization_batch",
                "source_batch_id": batch_id,
                "source_plan_task_id": plan_task_id,
                "feedback_case_ids": plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
                "eval_case_ids": batch.get("eval_case_ids") or [],
            },
        ) or task
        optimization_task_ids = self._unique_strings([*(batch.get("optimization_task_ids") or []), task["optimization_task_id"]])
        updated = self._update_batch_plan_task(
            batch_id,
            plan_task_id,
            {
                "status": "execution_planning",
                "internal_proposal_id": proposal_id,
                "optimization_task_id": task["optimization_task_id"],
            },
            batch_status="execution_planning",
            top_level_fields={
                "internal_proposal_id": batch.get("internal_proposal_id") or proposal_id,
                "optimization_task_id": batch.get("optimization_task_id") or task["optimization_task_id"],
                "optimization_task": task,
                "optimization_task_ids": optimization_task_ids,
            },
        )
        return {"batch": updated, "optimization_task": task, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}

    def notify_batch_plan_task_external(
        self,
        batch_id: str,
        plan_task_id: str,
        *,
        webhook_alias: str,
        sender: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        batch, plan, plan_task = self._batch_plan_task(batch_id, plan_task_id)
        if not batch or not plan or not plan_task:
            return None
        if plan_task.get("execution_kind") != "external_webhook":
            raise ValueError("Optimization plan task is not an external webhook task")
        item = self._upsert_external_governance_item_for_plan_task(batch, plan, plan_task)
        notified = self.notify_external_governance_item(item["external_item_id"], webhook_alias=webhook_alias, sender=sender)
        if not notified:
            return None
        updated = self._update_batch_plan_task(
            batch_id,
            plan_task_id,
            {
                "status": notified.get("status") or "notification_failed",
                "external_item_id": notified.get("external_item_id"),
                "latest_webhook_alias": notified.get("latest_webhook_alias"),
                "latest_notification": notified.get("latest_notification"),
            },
            batch_status=str(batch.get("status") or "pending_approval"),
        )
        return {"batch": updated, "external_item": notified, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}

    def record_batch_execution_result(
        self,
        batch_id: str,
        *,
        execution_job: Optional[dict[str, Any]] = None,
        optimization_task: Optional[dict[str, Any]] = None,
        applied: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        fields: dict[str, Any] = {}
        status = "execution_planning"
        if execution_job:
            fields["execution_job_id"] = execution_job.get("execution_job_id")
            fields["execution_job"] = execution_job
            status = "execution_ready" if execution_job.get("status") == "ready" else str(execution_job.get("status") or status)
        if optimization_task:
            fields["optimization_task_id"] = optimization_task.get("optimization_task_id")
            fields["optimization_task"] = optimization_task
            status = str(optimization_task.get("status") or status)
        if applied:
            fields["execution_apply_result"] = applied
            task = applied.get("optimization_task") if isinstance(applied.get("optimization_task"), dict) else None
            if task:
                fields["optimization_task"] = task
                status = str(task.get("status") or "applied_pending_regression")
        return self._update_batch(batch_id, status=status, fields=fields)

    def record_batch_plan_task_execution_result(
        self,
        batch_id: str,
        plan_task_id: str,
        *,
        execution_job: Optional[dict[str, Any]] = None,
        optimization_task: Optional[dict[str, Any]] = None,
        applied: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        task_updates: dict[str, Any] = {}
        top_level_fields: dict[str, Any] = {}
        status = "execution_planning"
        if execution_job:
            task_updates["execution_job_id"] = execution_job.get("execution_job_id")
            task_updates["latest_execution_job"] = execution_job
            status = "execution_ready" if execution_job.get("status") == "ready" else str(execution_job.get("status") or status)
            top_level_fields.update({"execution_job_id": execution_job.get("execution_job_id"), "execution_job": execution_job})
        if optimization_task:
            task_updates["optimization_task_id"] = optimization_task.get("optimization_task_id")
            task_updates["status"] = optimization_task.get("status") or status
            task_updates["applied_agent_version_id"] = optimization_task.get("applied_agent_version_id")
            status = str(optimization_task.get("status") or status)
            top_level_fields.update({"optimization_task_id": optimization_task.get("optimization_task_id"), "optimization_task": optimization_task})
        if applied:
            task_updates["execution_apply_result"] = applied
            top_level_fields["execution_apply_result"] = applied
            applied_job = applied.get("execution_job") if isinstance(applied.get("execution_job"), dict) else None
            if applied_job:
                task_updates["execution_job_id"] = applied_job.get("execution_job_id")
                task_updates["latest_execution_job"] = applied_job
                top_level_fields["execution_job_id"] = applied_job.get("execution_job_id")
                top_level_fields["execution_job"] = applied_job
            task = applied.get("optimization_task") if isinstance(applied.get("optimization_task"), dict) else None
            if task:
                task_updates["optimization_task_id"] = task.get("optimization_task_id")
                task_updates["status"] = task.get("status") or "applied_pending_regression"
                task_updates["applied_agent_version_id"] = task.get("applied_agent_version_id")
                top_level_fields["optimization_task"] = task
                status = str(task.get("status") or "applied_pending_regression")
        if "status" not in task_updates:
            task_updates["status"] = status
        return self._update_batch_plan_task(batch_id, plan_task_id, task_updates, batch_status=status, top_level_fields=top_level_fields)

    def record_batch_regression_result(self, batch_id: str, eval_run: dict[str, Any]) -> Optional[dict[str, Any]]:
        result_status = str(eval_run.get("result_status") or eval_run.get("status") or "needs_human_review")
        status = "completed" if result_status == "passed" else result_status
        return self._update_batch(
            batch_id,
            status=status,
            fields={"eval_run_id": eval_run.get("eval_run_id"), "latest_eval_run": eval_run},
        )

    def create_case(
        self,
        *,
        source_ids: list[str],
        title: Optional[str] = None,
        priority: str = "medium",
    ) -> Optional[dict[str, Any]]:
        unique_ids = self._unique_strings(source_ids)
        if not unique_ids:
            return None

        signals: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        unresolved: list[str] = []

        for source_id in unique_ids:
            signal = self.find_signal(source_id)
            if signal:
                signals.append(signal)
                continue
            event = self.find_event(source_id)
            if event:
                if not event.get("matched_run_id") and not event.get("run_id"):
                    return None
                events.append(event)
                continue
            pending_record = self.find_pending(source_id)
            if pending_record and pending_record.get("status") == "resolved":
                pending.append(pending_record)
                event = self.find_event(str(pending_record.get("event_id") or ""))
                if event:
                    events.append(event)
                continue
            unresolved.append(source_id)

        if unresolved:
            return None

        records = [*signals, *events, *pending]
        now = utc_now()
        feedback_case = self._scrub_record(
            {
                "feedback_case_id": f"fbc-{uuid.uuid4()}",
                "created_at": now,
                "updated_at": now,
                "status": "pending_evidence",
                "title": title or self._case_title(records),
                "priority": priority or "medium",
                "source_ids": unique_ids,
                "signal_ids": self._unique_strings([record.get("signal_id") for record in signals]),
                "event_ids": self._unique_strings([record.get("event_id") for record in events]),
                "pending_correlation_ids": self._unique_strings([record.get("pending_id") for record in pending]),
                "run_ids": self._unique_strings(
                    [
                        *[self._string(record.get("run_id")) or self._string(record.get("matched_run_id")) or "" for record in signals],
                        *[self._string(record.get("run_id")) or self._string(record.get("matched_run_id")) or "" for record in events],
                        *[self._string(record.get("resolved_run_id")) or "" for record in pending],
                    ]
                ),
                "session_ids": self._unique_strings([self._string(record.get("session_id")) or "" for record in records]),
                "alert_ids": self._unique_strings([self._string(record.get("alert_id")) or "" for record in records]),
                "case_ids": self._unique_strings([self._string(record.get("case_id")) or "" for record in records]),
                "evidence_package_ids": [],
                "attribution_job_ids": [],
                "proposal_job_ids": [],
            }
        )
        with self.Session.begin() as db:
            db.add(self._case_model_from_dict(feedback_case))
        return feedback_case

    def list_cases(
        self,
        *,
        status: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query_text = q.lower() if q else None
        result: list[dict[str, Any]] = []
        with self.Session() as db:
            rows = db.scalars(select(FeedbackCaseModel).order_by(FeedbackCaseModel.updated_at.desc())).all()
            for row in rows:
                record = self._case_to_dict(row)
                if status and record.get("status") != status:
                    continue
                if query_text and query_text not in json.dumps(record, ensure_ascii=False).lower():
                    continue
                result.append(record)
                if len(result) >= limit:
                    break
        return result

    def find_case(self, feedback_case_id: str) -> Optional[dict[str, Any]]:
        if not feedback_case_id:
            return None
        with self.Session() as db:
            record = db.get(FeedbackCaseModel, feedback_case_id)
            return self._case_to_dict(record) if record else None

    def create_evidence_package(self, feedback_case_id: str) -> Optional[dict[str, Any]]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        existing_id = self._latest(feedback_case.get("evidence_package_ids"))
        if existing_id:
            existing = self.get_evidence_package(existing_id)
            if existing:
                return existing

        evidence_id = f"evp-{uuid.uuid4()}"
        signals_clean = [item for item in (self.find_signal(source_id) for source_id in feedback_case.get("signal_ids", [])) if item]
        events_clean = [item for item in (self.find_event(source_id) for source_id in feedback_case.get("event_ids", [])) if item]
        runs_clean = [item for item in (self.find_run(run_id=run_id) for run_id in feedback_case.get("run_ids", [])) if item]
        sessions = [
            {
                "session_id": session_id,
                "run_ids": [run.get("run_id") for run in runs_clean if run.get("session_id") == session_id],
            }
            for session_id in feedback_case.get("session_ids", [])
        ]
        tool_calls = [
            call
            for run in runs_clean
            for call in (run.get("agent_activity") or {}).get("tool_calls", [])
            if isinstance(call, dict)
        ]
        messages = [
            {"run_id": run.get("run_id"), "session_id": run.get("session_id"), "messages": run.get("messages") or []}
            for run in runs_clean
        ]
        agent_activity = [
            {"run_id": run.get("run_id"), "session_id": run.get("session_id"), "agent_activity": run.get("agent_activity") or {}}
            for run in runs_clean
        ]
        langfuse_trace_refs = self._langfuse_trace_refs(runs_clean)
        trace_summary = [
            {
                "run_id": run.get("run_id"),
                "session_id": run.get("session_id"),
                "answer_summary": run.get("answer_summary"),
                "tool_names": (run.get("agent_activity") or {}).get("tool_names") or [],
                "errors": run.get("errors") or [],
                "langfuse_trace_id": run.get("langfuse_trace_id"),
                "langfuse_trace_url": run.get("langfuse_trace_url"),
            }
            for run in runs_clean
        ]
        main_agent_version = {"main_agent_version_id": self._current_agent_version_id(), "captured_at": utc_now()}
        redaction_report = {
            "enabled": not self.enable_debug_evidence,
            "policy": "debug-evidence-raw-v1" if self.enable_debug_evidence else "security-redaction-v1",
            "redacted_fields": list(SENSITIVE_KEY_PARTS),
        }

        files: dict[str, Any] = {
            "feedback.json": signals_clean,
            "runs.json": runs_clean,
            "sessions.json": sessions,
            "tool_calls.json": tool_calls,
            "soc_events.json": events_clean,
            "trace_summary.json": trace_summary,
            "main_agent_version.json": main_agent_version,
            "redaction_report.json": redaction_report,
        }
        if self.enable_debug_evidence:
            files.update(
                {
                    "messages.json": messages,
                    "agent_activity.json": agent_activity,
                    "langfuse_trace_refs.json": langfuse_trace_refs,
                }
            )
        included_files = [
            {
                "path": name,
                "sha256": self._sha256_json(self._evidence_payload(payload)),
                "type": name.removesuffix(".json"),
            }
            for name, payload in files.items()
        ]
        trace_ids = self._unique_strings([item.get("trace_id") for item in langfuse_trace_refs])
        manifest = {
            "schema_version": "evidence-package/v1",
            "evidence_package_id": evidence_id,
            "feedback_case_id": feedback_case_id,
            "created_at": utc_now(),
            "created_by": "system",
            "main_agent_version_id": main_agent_version["main_agent_version_id"],
            "source_refs": {
                "feedback_ids": feedback_case.get("signal_ids", []),
                "signal_ids": feedback_case.get("signal_ids", []),
                "run_ids": feedback_case.get("run_ids", []),
                "session_ids": feedback_case.get("session_ids", []),
                "trace_ids": trace_ids,
                "alert_ids": feedback_case.get("alert_ids", []),
                "case_ids": feedback_case.get("case_ids", []),
                "event_ids": feedback_case.get("event_ids", []),
            },
            "included_files": included_files,
            "redaction": redaction_report,
            "completeness": {
                "has_feedback": bool(signals_clean),
                "has_runs": bool(runs_clean),
                "has_tool_calls": bool(tool_calls),
                "has_trace_summary": bool(trace_summary),
                "has_main_agent_version": bool(main_agent_version["main_agent_version_id"]),
                "has_messages": bool(messages and any(item.get("messages") for item in messages)),
                "has_agent_activity": bool(agent_activity and any(item.get("agent_activity") for item in agent_activity)),
                "has_langfuse_trace_refs": bool(langfuse_trace_refs),
                "has_langfuse_trace_details": False,
            },
        }
        with self.Session.begin() as db:
            db.add(EvidencePackageModel(evidence_package_id=evidence_id, feedback_case_id=feedback_case_id, created_at=manifest["created_at"], manifest_json=manifest))
            db.flush()
            for item in included_files:
                content = self._evidence_payload(files[item["path"]])
                db.add(
                    EvidenceFileModel(
                        evidence_package_id=evidence_id,
                        file_name=item["path"],
                        file_type=item["type"],
                        sha256=item["sha256"],
                        content_json=content,
                    )
                )
        self._append_case_update(feedback_case, evidence_package_id=evidence_id, status="pending_attribution")
        return manifest

    def get_evidence_package(self, evidence_package_id: str) -> Optional[dict[str, Any]]:
        if not evidence_package_id:
            return None
        with self.Session() as db:
            record = db.get(EvidencePackageModel, evidence_package_id)
            return record.manifest_json if record else None

    def get_evidence_package_file(self, evidence_package_id: str, file_name: str) -> Optional[dict[str, Any]]:
        if not file_name or Path(file_name).name != file_name or file_name == "manifest.json":
            return None
        with self.Session() as db:
            record = db.get(EvidenceFileModel, {"evidence_package_id": evidence_package_id, "file_name": file_name})
            if not record:
                return None
            return {
                "evidence_package_id": evidence_package_id,
                "file_name": file_name,
                "sha256": record.sha256,
                "content": record.content_json,
            }

    def create_attribution_job(
        self,
        feedback_case_id: str,
        *,
        evidence_package_id: Optional[str] = None,
        profile_version: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        if force:
            self.discard_current_attribution(feedback_case_id, invalidate_downstream=True)
            feedback_case = self.find_case(feedback_case_id)
            if not feedback_case:
                return None
        existing = None if force else self._latest_reusable_job(feedback_case_id, "attribution")
        if existing:
            return {**existing, "_reused_existing": True}
        evidence_package_id = evidence_package_id or self._latest(feedback_case.get("evidence_package_ids"))
        if not evidence_package_id:
            manifest = self.create_evidence_package(feedback_case_id)
            evidence_package_id = self._string(manifest.get("evidence_package_id")) if manifest else None
            feedback_case = self.find_case(feedback_case_id) or feedback_case
        if not evidence_package_id:
            return None

        job_id = f"fba-{uuid.uuid4()}"
        allowed_evidence_paths = self._materialize_evidence_files(
            job_id,
            "attribution",
            evidence_package_id,
            (
                "feedback.json",
                "tool_calls.json",
                "trace_summary.json",
                "soc_events.json",
                "main_agent_version.json",
                "messages.json",
                "agent_activity.json",
                "langfuse_trace_refs.json",
            ),
        )
        input_payload = {
            "schema_version": "attribution-input/v1",
            "job_id": job_id,
            "feedback_case_id": feedback_case_id,
            "evidence_package_id": evidence_package_id,
            "main_agent_version_id": self._current_agent_version_id(),
            "evidence_manifest_path": self._materialize_manifest(job_id, "attribution", evidence_package_id),
            "allowed_evidence_paths": allowed_evidence_paths,
            "task": "analyze_feedback_attribution",
        }
        input_path = self._write_job_input(job_id, "attribution", input_payload)
        job = self._job_record(
            job_id=job_id,
            job_type="attribution",
            feedback_case_id=feedback_case_id,
            evidence_package_id=evidence_package_id,
            status="queued",
            profile_name="attribution-analyzer",
            input_path=input_path,
            profile_version=profile_version,
        )
        job["input_json"] = input_payload
        with self.Session.begin() as db:
            db.add(self._job_model_from_dict(job))
        self._append_case_update(feedback_case, attribution_job_id=job_id, status="attribution_queued")
        return self.get_job(job_id)

    def create_proposal_job(
        self,
        feedback_case_id: str,
        *,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
        profile_version: Optional[dict[str, Any]] = None,
        force: bool = False,
        regeneration_instruction: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        regeneration_instruction = regeneration_instruction.strip() if isinstance(regeneration_instruction, str) else None
        regeneration_instruction = regeneration_instruction or None
        existing = None if force or regeneration_instruction else self._latest_reusable_job(feedback_case_id, "proposal")
        if existing:
            return {**existing, "_reused_existing": True}
        evidence_package_id = evidence_package_id or self._latest(feedback_case.get("evidence_package_ids"))
        attribution_job_id = attribution_job_id or self._latest(feedback_case.get("attribution_job_ids"))
        if not evidence_package_id or not attribution_job_id:
            return None
        attribution_output = self.get_job_output(attribution_job_id, "attribution")
        if not attribution_output:
            return None

        job_id = f"fbp-{uuid.uuid4()}"
        if force:
            self._supersede_case_proposals(
                feedback_case_id,
                reason="proposal_regenerated",
                superseded_by_job_id=job_id,
            )
        attribution_output_path = self._materialize_extra_json(job_id, "proposal", "attribution_validated_output.json", attribution_output)
        input_payload = {
            "schema_version": "proposal-input/v1",
            "job_id": job_id,
            "feedback_case_id": feedback_case_id,
            "evidence_package_id": evidence_package_id,
            "attribution_job_id": attribution_job_id,
            "attribution_output_path": attribution_output_path,
            "main_agent_version_id": self._current_agent_version_id(),
            "main_agent_manifest_path": str(self.data_dir / "agent-versions" / "main" / "current.json"),
            "allowed_target_paths": ["<any-managed-main-workspace-relative-file>"],
            "target_policy": self._execution_target_policy(),
            "task": "generate_optimization_proposals",
        }
        if regeneration_instruction:
            input_payload["regeneration_instruction"] = regeneration_instruction
        input_path = self._write_job_input(job_id, "proposal", input_payload)
        job = self._job_record(
            job_id=job_id,
            job_type="proposal",
            feedback_case_id=feedback_case_id,
            evidence_package_id=evidence_package_id,
            status="queued",
            profile_name="proposal-generator",
            input_path=input_path,
            profile_version=profile_version,
            attribution_job_id=attribution_job_id,
        )
        job["input_json"] = input_payload
        with self.Session.begin() as db:
            db.add(self._job_model_from_dict(job))
        self._append_case_update(feedback_case, proposal_job_id=job_id, status="proposal_queued")
        return self.get_job(job_id)

    def revalidate_proposal_job(self, job_id: str) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job or job.get("job_type") != "proposal":
            return None
        raw_output = job.get("raw_output_json")
        if not isinstance(raw_output, dict):
            return None
        self._append_job_update(job_id, status="schema_validating")
        validated, error = validate_proposal_output(raw_output)
        if not validated:
            self._write_job_error(job, "SCHEMA_VALIDATION_FAILED", error or "invalid proposal output")
            feedback_case = self.find_case(str(job["feedback_case_id"]))
            if feedback_case:
                self._append_case_update(feedback_case, status="needs_human_review")
            completed = self._append_job_update(job_id, status="needs_human_review", completed_at=utc_now())
            self._cleanup_job_tmp(job_id)
            return completed

        normalized = self._normalize_proposal_output(validated, job)
        normalized["external_guidance"] = self._upsert_external_governance_items(normalized, job)
        with self.Session.begin() as db:
            row = db.get(FeedbackJobModel, job_id)
            if not row:
                return None
            row.status = "completed"
            row.completed_at = utc_now()
            row.validated_output_json = normalized
            row.error_json = None
            for proposal in normalized.get("proposals", []):
                db.merge(self._proposal_model_from_dict(proposal))
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        if feedback_case:
            self._append_case_update(
                feedback_case,
                status="pending_review" if normalized.get("proposals") else "needs_human_review",
            )
        self._cleanup_job_tmp(job_id)
        return self.get_job(job_id)

    def start_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return self._append_job_update(job_id, status="running", started_at=utc_now())

    def complete_attribution_job(self, job_id: str, raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return None
        self._set_job_json(job_id, raw_output_json=raw_output)
        self._append_job_update(job_id, status="schema_validating")
        validated, error = validate_attribution_output(raw_output)
        if not validated:
            self._write_job_error(job, "SCHEMA_VALIDATION_FAILED", error or "invalid attribution output")
            feedback_case = self.find_case(str(job["feedback_case_id"]))
            if feedback_case:
                self._append_case_update(feedback_case, status="needs_human_review")
            completed = self._append_job_update(job_id, status="needs_human_review", completed_at=utc_now())
            self._cleanup_job_tmp(job_id)
            return completed
        self._set_job_json(job_id, validated_output_json=validated)
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        if feedback_case:
            self._append_case_update(feedback_case, status="pending_proposal")
        completed = self._append_job_update(job_id, status="completed", completed_at=utc_now())
        self._cleanup_job_tmp(job_id)
        return completed

    def complete_proposal_job(self, job_id: str, raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return None
        self._set_job_json(job_id, raw_output_json=raw_output)
        self._append_job_update(job_id, status="schema_validating")
        validated, error = validate_proposal_output(raw_output)
        if not validated:
            self._write_job_error(job, "SCHEMA_VALIDATION_FAILED", error or "invalid proposal output")
            feedback_case = self.find_case(str(job["feedback_case_id"]))
            if feedback_case:
                self._append_case_update(feedback_case, status="needs_human_review")
            completed = self._append_job_update(job_id, status="needs_human_review", completed_at=utc_now())
            self._cleanup_job_tmp(job_id)
            return completed

        normalized = self._normalize_proposal_output(validated, job)
        normalized["external_guidance"] = self._upsert_external_governance_items(normalized, job)
        self._set_job_json(job_id, validated_output_json=normalized)
        with self.Session.begin() as db:
            for proposal in normalized.get("proposals", []):
                db.merge(self._proposal_model_from_dict(proposal))
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        if feedback_case:
            self._append_case_update(
                feedback_case,
                status="pending_review" if normalized.get("proposals") else "needs_human_review",
            )
        completed = self._append_job_update(job_id, status="completed", completed_at=utc_now())
        self._cleanup_job_tmp(job_id)
        return completed

    def fail_job(self, job_id: str, *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return None
        self._write_job_error(job, error_code, message)
        failed = self._append_job_update(job_id, status="failed", completed_at=utc_now())
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        if feedback_case:
            if job.get("job_type") == "attribution":
                self._append_case_update(feedback_case, status="pending_attribution")
            elif job.get("job_type") == "proposal":
                self._append_case_update(feedback_case, status="pending_proposal")
        if job.get("job_type") == "batch_plan":
            batch_id = self._job_batch_id(job)
            if batch_id:
                self._update_batch(
                    batch_id,
                    status="needs_human_review",
                    fields={
                        "optimization_plan_job_id": job_id,
                        "optimization_plan_job": failed,
                        "optimization_plan_error": (failed or {}).get("error_json"),
                    },
                )
        self._cleanup_job_tmp(job_id)
        return failed

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        if not job_id:
            return None
        with self.Session() as db:
            row = db.get(FeedbackJobModel, job_id)
            return self._job_to_dict(row) if row else None

    def get_job_output(self, job_id: str, job_type: str) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job or job.get("job_type") != job_type:
            return None
        output = job.get("validated_output_json")
        return output if isinstance(output, dict) else None

    def discard_current_attribution(self, feedback_case_id: str, *, invalidate_downstream: bool = True) -> Optional[dict[str, Any]]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        attribution_job_id = self._latest(feedback_case.get("attribution_job_ids"))
        proposal_job_id = self._latest(feedback_case.get("proposal_job_ids")) if invalidate_downstream else None
        if attribution_job_id:
            self._discard_job(attribution_job_id)
        if proposal_job_id:
            self._discard_proposal_job(proposal_job_id)
        with self.Session.begin() as db:
            row = db.get(FeedbackCaseModel, feedback_case_id)
            if not row:
                return feedback_case
            row.updated_at = utc_now()
            row.status = "pending_attribution"
            row.current_attribution_job_id = None
            if invalidate_downstream:
                row.current_proposal_job_id = None
        return self.find_case(feedback_case_id)

    def list_proposals(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {"feedback_case_id": feedback_case_id, "status": status}
        with self.Session() as db:
            proposals = [self._proposal_to_dict(row) for row in db.scalars(select(OptimizationProposalModel).order_by(OptimizationProposalModel.created_at.desc())).all()]
        if status is None:
            proposals = [item for item in proposals if item.get("status") != "superseded"]
        return self._filter_records(proposals, filters, limit)

    def find_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]:
        if not proposal_id:
            return None
        with self.Session() as db:
            row = db.get(OptimizationProposalModel, proposal_id)
            return self._proposal_to_dict(row) if row else None

    def review_proposal(self, proposal_id: str, *, action: str, comment: Optional[str] = None) -> Optional[dict[str, Any]]:
        proposal = self.find_proposal(proposal_id)
        if not proposal:
            return None
        status_by_action = {"approve": "approved", "reject": "rejected", "request_more_analysis": "needs_more_analysis"}
        next_status = status_by_action[action]
        review = self._scrub_record(
            {
                "review_id": f"opr-{uuid.uuid4()}",
                "proposal_id": proposal_id,
                "created_at": utc_now(),
                "action": action,
                "status": next_status,
                "comment": comment,
            }
        )
        with self.Session.begin() as db:
            db.add(
                ProposalReviewModel(
                    review_id=review["review_id"],
                    proposal_id=proposal_id,
                    created_at=review["created_at"],
                    action=action,
                    status=next_status,
                    payload_json=review,
                )
            )
            row = db.get(OptimizationProposalModel, proposal_id)
            if row:
                row.status = next_status
                row.payload_json = {**row.payload_json, "status": next_status, "latest_review": review}
        updated = self.find_proposal(proposal_id) or {**proposal, "status": next_status, "latest_review": review}
        return {"proposal": updated, "review": review}

    def create_task(self, *, proposal_id: str, execution_mode: str = "manual_or_patch", comment: Optional[str] = None) -> Optional[dict[str, Any]]:
        proposal = self.find_proposal(proposal_id)
        if not proposal or proposal.get("status") != "approved":
            return None
        if proposal.get("actionability") == "external_guidance":
            return None
        target_path = self._string(proposal.get("target_path"))
        if not target_path or not self._target_allowed(target_path):
            return None
        existing_task = self._find_latest_task_for_proposal(proposal_id)
        if existing_task:
            return existing_task
        task = self._scrub_record(
            {
                "optimization_task_id": f"opt-{uuid.uuid4()}",
                "created_at": utc_now(),
                "status": "pending_execution",
                "proposal_id": proposal_id,
                "proposal_ids": [proposal_id],
                "feedback_case_id": proposal.get("feedback_case_id"),
                "execution_mode": execution_mode,
                "source": "feedback_workbench",
                "comment": comment,
                "target_paths": [target_path],
                "proposal": proposal,
                "baseline_agent_version_id": proposal.get("base_agent_version_id") or self._current_agent_version_id(),
                "execution_job_ids": [],
                "latest_execution_job_id": None,
                "latest_execution_job": None,
            }
        )
        with self.Session.begin() as db:
            db.add(
                OptimizationTaskModel(
                    optimization_task_id=task["optimization_task_id"],
                    created_at=task["created_at"],
                    status=task["status"],
                    proposal_id=proposal_id,
                    feedback_case_id=self._string(task.get("feedback_case_id")),
                    payload_json=task,
                )
            )
        return task

    def _find_latest_task_for_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]:
        with self.Session() as db:
            row = db.scalars(
                select(OptimizationTaskModel)
                .where(OptimizationTaskModel.proposal_id == proposal_id)
                .order_by(OptimizationTaskModel.created_at.desc())
            ).first()
            return row.payload_json if row else None

    def list_tasks(self, *, feedback_case_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as db:
            tasks = [row.payload_json for row in db.scalars(select(OptimizationTaskModel).order_by(OptimizationTaskModel.created_at.desc())).all()]
        return self._filter_records(tasks, {"feedback_case_id": feedback_case_id, "status": status}, limit)

    def target_allowed(self, target_path: str) -> bool:
        return self._target_allowed(target_path)

    def find_task(self, task_id: str) -> Optional[dict[str, Any]]:
        if not task_id:
            return None
        with self.Session() as db:
            row = db.get(OptimizationTaskModel, task_id)
            return row.payload_json if row else None

    def create_execution_job(
        self,
        task_id: str,
        *,
        profile_version: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        task = self.find_task(task_id)
        if not task or task.get("applied_agent_version_id"):
            return None
        proposal = task.get("proposal") if isinstance(task.get("proposal"), dict) else None
        if not proposal or proposal.get("status") != "approved":
            return None
        if proposal.get("actionability") not in {"direct_workspace_change", "workspace_config_change"}:
            return None
        target_paths = [str(path) for path in task.get("target_paths") or [] if isinstance(path, str)]
        if not target_paths or any(not self._target_allowed(path) for path in target_paths):
            return None
        if not force:
            existing = self._latest_execution_job(task_id)
            if existing and existing.get("status") in {"queued", "running", "ready", "needs_human_review"}:
                return {**existing, "_reused_existing": True}
        job_id = f"fbe-{uuid.uuid4()}"
        baseline_version_id = self._string(task.get("baseline_agent_version_id")) or self._current_agent_version_id()
        input_payload = {
            "schema_version": "execution-input/v1",
            "execution_job_id": job_id,
            "optimization_task_id": task_id,
            "feedback_case_id": task.get("feedback_case_id"),
            "proposal_id": task.get("proposal_id"),
            "proposal": proposal,
            "target_paths": target_paths,
            "allowed_target_paths": target_paths,
            "target_policy": self._execution_target_policy(),
            "target_file_contexts": self._execution_target_file_contexts(target_paths),
            "baseline_agent_version_id": baseline_version_id,
            "current_agent_version_id": self._current_agent_version_id(),
            "main_agent_manifest_path": str(self.data_dir / "agent-versions" / "main" / "current.json"),
            "task": "generate_controlled_execution_plan",
        }
        input_path = self._write_job_input(job_id, "execution", input_payload)
        now = utc_now()
        job = self._scrub_record(
            {
                "execution_job_id": job_id,
                "optimization_task_id": task_id,
                "feedback_case_id": task.get("feedback_case_id"),
                "proposal_id": task.get("proposal_id"),
                "status": "queued",
                "profile_name": "execution-optimizer",
                "created_at": now,
                "started_at": None,
                "completed_at": None,
                "baseline_agent_version_id": baseline_version_id,
                "input_path": input_path,
                "input_json": input_payload,
                "raw_output_json": None,
                "validated_output_json": None,
                "error_json": None,
                "profile_version": profile_version,
            }
        )
        with self.Session.begin() as db:
            db.add(
                OptimizationExecutionModel(
                    execution_job_id=job_id,
                    optimization_task_id=task_id,
                    feedback_case_id=self._string(task.get("feedback_case_id")),
                    proposal_id=self._string(task.get("proposal_id")),
                    status="queued",
                    profile_name="execution-optimizer",
                    created_at=now,
                    baseline_agent_version_id=baseline_version_id,
                    payload_json=job,
                )
            )
        self._attach_execution_job_to_task(task_id, job, status="execution_planning")
        return self.get_execution_job(job_id)

    def start_execution_job(self, execution_job_id: str) -> Optional[dict[str, Any]]:
        return self._update_execution_job_payload(execution_job_id, status="running", fields={"started_at": utc_now()})

    def complete_execution_job(self, execution_job_id: str, raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        job = self.get_execution_job(execution_job_id)
        if not job:
            return None
        output = self._execution_output_with_job_context(raw_output, job)
        validated, error = validate_execution_plan_output(output)
        if not validated:
            failed = self.fail_execution_job(execution_job_id, "SCHEMA_VALIDATION_FAILED", error or "invalid execution output")
            return failed
        sanitized, sanitize_error = self._sanitize_execution_plan(validated, job)
        if sanitize_error:
            failed = self.fail_execution_job(execution_job_id, "EXECUTION_PLAN_UNSAFE", sanitize_error)
            return failed
        next_status = "ready" if sanitized.get("status") == "ready" else "needs_human_review"
        updated = self._update_execution_job_payload(
            execution_job_id,
            status=next_status,
            fields={
                "completed_at": utc_now(),
                "raw_output_json": raw_output,
                "validated_output_json": sanitized,
                "error_json": None,
            },
        )
        if updated:
            self._attach_execution_job_to_task(
                str(updated["optimization_task_id"]),
                updated,
                status="execution_ready" if next_status == "ready" else "needs_human_review",
            )
        return updated

    def _execution_output_with_job_context(self, raw_output: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        output = dict(raw_output)
        output["execution_job_id"] = self._string(output.get("execution_job_id")) or self._string(job.get("execution_job_id"))
        output["optimization_task_id"] = self._string(output.get("optimization_task_id")) or self._string(job.get("optimization_task_id"))
        output["baseline_agent_version_id"] = self._string(output.get("baseline_agent_version_id")) or self._string(job.get("baseline_agent_version_id"))
        return output

    def fail_execution_job(self, execution_job_id: str, error_code: str, message: str) -> Optional[dict[str, Any]]:
        error_payload = {"error_code": error_code, "message": message, "created_at": utc_now(), "execution_job_id": execution_job_id}
        failed = self._update_execution_job_payload(
            execution_job_id,
            status="failed",
            fields={"completed_at": utc_now(), "error_json": error_payload},
        )
        if failed:
            self._attach_execution_job_to_task(str(failed["optimization_task_id"]), failed, status="execution_failed")
        return failed

    def mark_execution_job_applied(
        self,
        execution_job_id: str,
        *,
        pre_execution_version: dict[str, Any],
        applied_agent_version: dict[str, Any],
        applied_diff: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        job = self.get_execution_job(execution_job_id)
        if not job:
            return None
        fields = {
            "completed_at": utc_now(),
            "pre_execution_agent_version_id": self._string(pre_execution_version.get("agent_version_id")),
            "pre_execution_agent_version": pre_execution_version,
            "applied_agent_version_id": self._string(applied_agent_version.get("agent_version_id")),
            "applied_agent_version": applied_agent_version,
            "applied_diff": applied_diff or {},
        }
        updated_job = self._update_execution_job_payload(execution_job_id, status="completed", fields=fields)
        if updated_job:
            self._attach_execution_job_to_task(str(updated_job["optimization_task_id"]), updated_job, status="applied_pending_regression")
            self.mark_task_applied(
                str(updated_job["optimization_task_id"]),
                agent_version=applied_agent_version,
                note=f"execution-optimizer 应用执行方案 {execution_job_id}。",
                pre_execution_version=pre_execution_version,
                execution_job=updated_job,
            )
        return updated_job

    def get_execution_job(self, execution_job_id: str) -> Optional[dict[str, Any]]:
        if not execution_job_id:
            return None
        with self.Session() as db:
            row = db.get(OptimizationExecutionModel, execution_job_id)
            return self._execution_job_to_dict(row) if row else None

    def list_execution_jobs(self, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as db:
            rows = db.scalars(
                select(OptimizationExecutionModel)
                .where(OptimizationExecutionModel.optimization_task_id == task_id)
                .order_by(OptimizationExecutionModel.created_at.desc())
            ).all()
        return [self._execution_job_to_dict(row) for row in rows[:limit]]

    def list_external_webhooks(self) -> list[dict[str, Any]]:
        if not self.external_webhooks_path.exists():
            return []
        try:
            loaded = yaml.safe_load(self.external_webhooks_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid external governance webhook config: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("External governance webhook config must be a mapping")
        webhooks = loaded.get("webhooks") or []
        if not isinstance(webhooks, list):
            raise ValueError("External governance webhook config field webhooks must be a list")
        normalized: list[dict[str, Any]] = []
        for item in webhooks:
            if not isinstance(item, dict):
                continue
            alias = self._string(item.get("alias"))
            url = self._string(item.get("url"))
            if not alias or not url:
                continue
            normalized.append(
                {
                    "alias": alias,
                    "name": self._string(item.get("name")) or alias,
                    "url": url,
                    "has_token": bool(self._string(item.get("token"))),
                }
            )
        return normalized

    def list_external_governance_items(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.Session() as db:
            rows = db.scalars(select(ExternalGovernanceItemModel).order_by(ExternalGovernanceItemModel.created_at.desc())).all()
            items = [self._external_governance_item_to_dict(row) for row in rows]
        if status is None:
            items = [item for item in items if item.get("status") != "superseded"]
        return self._filter_records(items, {"feedback_case_id": feedback_case_id, "proposal_job_id": proposal_job_id, "status": status}, limit)

    def find_external_governance_item(self, external_item_id: str) -> Optional[dict[str, Any]]:
        if not external_item_id:
            return None
        with self.Session() as db:
            row = db.get(ExternalGovernanceItemModel, external_item_id)
            return self._external_governance_item_to_dict(row) if row else None

    def notify_external_governance_item(
        self,
        external_item_id: str,
        *,
        webhook_alias: str,
        sender: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        item = self.find_external_governance_item(external_item_id)
        if not item:
            return None
        webhook = self._external_webhook_by_alias(webhook_alias)
        payload = self._external_notification_payload(item, webhook)
        notification_id = f"egn-{uuid.uuid4()}"
        created_at = utc_now()
        notification = {
            "notification_id": notification_id,
            "external_item_id": external_item_id,
            "created_at": created_at,
            "completed_at": None,
            "status": "sending",
            "webhook_alias": webhook["alias"],
            "request_json": payload,
            "http_status": None,
            "response_body": None,
            "error": None,
        }
        try:
            response = (sender or self._send_external_webhook)(webhook, payload)
            http_status = int(response.get("http_status") or 0)
            response_body = self._truncate(self._string(response.get("response_body")) or "")
            notification.update(
                {
                    "completed_at": utc_now(),
                    "status": "sent" if 200 <= http_status < 300 else "failed",
                    "http_status": http_status,
                    "response_body": response_body,
                }
            )
        except Exception as exc:
            notification.update({"completed_at": utc_now(), "status": "failed", "error": str(exc)})

        item_status = "notified" if notification["status"] == "sent" else "notification_failed"
        with self.Session.begin() as db:
            db.add(
                ExternalNotificationModel(
                    notification_id=notification_id,
                    external_item_id=external_item_id,
                    created_at=notification["created_at"],
                    completed_at=notification["completed_at"],
                    status=notification["status"],
                    webhook_alias=webhook["alias"],
                    http_status=notification["http_status"],
                    payload_json=notification,
                )
            )
            row = db.get(ExternalGovernanceItemModel, external_item_id)
            if row:
                row.status = item_status
                row.updated_at = utc_now()
                row.latest_notification_id = notification_id
                row.payload_json = {
                    **(row.payload_json or {}),
                    "status": item_status,
                    "updated_at": row.updated_at,
                    "latest_notification_id": notification_id,
                    "latest_webhook_alias": webhook["alias"],
                    "latest_notification": notification,
                }
        return self.find_external_governance_item(external_item_id)

    def mark_task_applied(
        self,
        task_id: str,
        *,
        agent_version: dict[str, Any],
        note: Optional[str] = None,
        pre_execution_version: Optional[dict[str, Any]] = None,
        execution_job: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        task = self.find_task(task_id)
        if not task:
            return None
        if task.get("applied_agent_version_id"):
            return task
        fields = {
            "applied_at": utc_now(),
            "applied_agent_version_id": self._string(agent_version.get("agent_version_id")),
            "applied_agent_version": agent_version,
            "application_note": note,
        }
        if pre_execution_version:
            fields["pre_execution_agent_version_id"] = self._string(pre_execution_version.get("agent_version_id"))
            fields["pre_execution_agent_version"] = pre_execution_version
        if execution_job:
            fields["latest_execution_job_id"] = execution_job.get("execution_job_id")
            fields["latest_execution_job"] = execution_job
        return self._update_task_payload(
            task_id,
            status="applied_pending_regression",
            fields=fields,
        )

    def update_task_status(
        self,
        task_id: str,
        *,
        status: str,
        fields: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        return self._update_task_payload(task_id, status=status, fields=fields or {})

    def sync_feedback_eval_cases(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        feedback_cases = [self.find_case(feedback_case_id)] if feedback_case_id else self.list_cases(limit=limit)
        created = 0
        reused = 0
        skipped = 0
        eval_cases: list[dict[str, Any]] = []
        for feedback_case in feedback_cases:
            if not feedback_case:
                skipped += 1
                continue
            existing = self.find_eval_case(source_feedback_case_id=feedback_case["feedback_case_id"])
            if existing:
                reused += 1
                eval_cases.append(existing)
                continue
            payload = self._build_eval_case_from_feedback(feedback_case)
            if not payload:
                skipped += 1
                continue
            with self.Session.begin() as db:
                db.add(
                    EvalCaseModel(
                        eval_case_id=payload["eval_case_id"],
                        created_at=payload["created_at"],
                        updated_at=payload["updated_at"],
                        status=payload["status"],
                        source_feedback_case_id=self._string(payload.get("source_feedback_case_id")),
                        source_run_id=self._string(payload.get("source_run_id")),
                        labels_json=list(payload.get("labels") or []),
                        payload_json=payload,
                    )
                )
            created += 1
            eval_cases.append(payload)
        return {"created": created, "reused": reused, "skipped": skipped, "eval_cases": eval_cases}

    def list_eval_cases(
        self,
        *,
        status: Optional[str] = None,
        source_feedback_case_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {"status": status, "source_feedback_case_id": source_feedback_case_id}
        with self.Session() as db:
            cases = [self._eval_case_to_dict(row) for row in db.scalars(select(EvalCaseModel).order_by(EvalCaseModel.updated_at.desc())).all()]
        return self._filter_records(cases, filters, limit)

    def find_eval_case(
        self,
        eval_case_id: Optional[str] = None,
        *,
        source_feedback_case_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with self.Session() as db:
            row: EvalCaseModel | None = None
            if eval_case_id:
                row = db.get(EvalCaseModel, eval_case_id)
            elif source_feedback_case_id:
                row = db.scalars(
                    select(EvalCaseModel)
                    .where(EvalCaseModel.source_feedback_case_id == source_feedback_case_id)
                    .order_by(EvalCaseModel.updated_at.desc())
                ).first()
            return self._eval_case_to_dict(row) if row else None

    def update_eval_case(self, eval_case_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        updated_at = utc_now()
        with self.Session.begin() as db:
            row = db.get(EvalCaseModel, eval_case_id)
            if not row:
                return None
            payload = dict(row.payload_json or {})

            if "prompt" in fields:
                prompt = self._string(fields.get("prompt")).strip()
                if not prompt:
                    raise ValueError("Eval case prompt cannot be empty")
                payload["prompt"] = prompt
            if "expected_behavior" in fields:
                payload["expected_behavior"] = self._string(fields.get("expected_behavior")).strip()
            if "checks_json" in fields:
                checks = fields.get("checks_json")
                if checks is not None and not isinstance(checks, dict):
                    raise ValueError("Eval case checks_json must be an object")
                payload["checks_json"] = dict(checks or {})
            if "labels" in fields:
                labels = fields.get("labels")
                if labels is not None and not isinstance(labels, list):
                    raise ValueError("Eval case labels must be a list")
                normalized_labels = self._unique_strings([str(item).strip() for item in labels or [] if str(item).strip()])
                payload["labels"] = normalized_labels
                row.labels_json = normalized_labels
            if "status" in fields:
                new_status = self._string(fields.get("status")).strip()
                if new_status not in {"active", "draft", "archived"}:
                    raise ValueError("Eval case status must be active, draft, or archived")
                payload["status"] = new_status
                row.status = new_status

            payload["updated_at"] = updated_at
            row.updated_at = updated_at
            row.payload_json = payload
        return self.find_eval_case(eval_case_id)

    def create_eval_run(
        self,
        *,
        eval_case_ids: list[str],
        agent_version_id: Optional[str],
        optimization_task_id: Optional[str] = None,
        source: str = "manual_feedback_dataset",
    ) -> dict[str, Any]:
        created_at = utc_now()
        payload = {
            "eval_run_id": f"evr-{uuid.uuid4()}",
            "created_at": created_at,
            "completed_at": None,
            "status": "running",
            "result_status": "running",
            "agent_version_id": agent_version_id,
            "optimization_task_id": optimization_task_id,
            "source": source,
            "eval_case_ids": eval_case_ids,
            "item_ids": [],
            "summary": {"total": len(eval_case_ids), "passed": 0, "failed": 0, "needs_human_review": 0},
        }
        with self.Session.begin() as db:
            db.add(
                EvalRunModel(
                    eval_run_id=payload["eval_run_id"],
                    created_at=created_at,
                    completed_at=None,
                    status="running",
                    agent_version_id=self._string(agent_version_id),
                    optimization_task_id=self._string(optimization_task_id),
                    source=source,
                    payload_json=payload,
                )
            )
        return payload

    def append_eval_run_item(
        self,
        eval_run_id: str,
        *,
        eval_case: dict[str, Any],
        agent_result: Optional[dict[str, Any]],
        status: str,
        score: float,
        check_results: list[dict[str, Any]],
        error_json: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        if not self.get_eval_run(eval_run_id):
            return None
        item_id = f"evi-{uuid.uuid4()}"
        answer = self._string((agent_result or {}).get("answer"))
        payload = {
            "eval_run_item_id": item_id,
            "eval_run_id": eval_run_id,
            "eval_case_id": eval_case["eval_case_id"],
            "source_feedback_case_id": eval_case.get("source_feedback_case_id"),
            "agent_run_id": (agent_result or {}).get("run_id"),
            "agent_version_id": (agent_result or {}).get("agent_version_id"),
            "status": status,
            "score": score,
            "check_results": check_results,
            "answer_summary": answer.strip().replace("\n", " ")[:500],
            "error_json": error_json,
            "created_at": utc_now(),
        }
        with self.Session.begin() as db:
            db.add(
                EvalRunItemModel(
                    eval_run_item_id=item_id,
                    eval_run_id=eval_run_id,
                    eval_case_id=eval_case["eval_case_id"],
                    agent_run_id=self._string(payload.get("agent_run_id")),
                    status=status,
                    score=score,
                    payload_json=payload,
                )
            )
            run = db.get(EvalRunModel, eval_run_id)
            if run:
                current = dict(run.payload_json or {})
                current["item_ids"] = [*list(current.get("item_ids") or []), item_id]
                run.payload_json = current
        return payload

    def finish_eval_run(self, eval_run_id: str) -> Optional[dict[str, Any]]:
        completed_at = utc_now()
        with self.Session.begin() as db:
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            items = list(db.scalars(select(EvalRunItemModel).where(EvalRunItemModel.eval_run_id == eval_run_id)).all())
            summary = {
                "total": len(items),
                "passed": sum(1 for item in items if item.status == "passed"),
                "failed": sum(1 for item in items if item.status == "failed"),
                "needs_human_review": sum(1 for item in items if item.status == "needs_human_review"),
            }
            if summary["failed"]:
                result_status = "failed"
            elif summary["needs_human_review"]:
                result_status = "needs_human_review"
            elif summary["passed"] == summary["total"] and summary["total"]:
                result_status = "passed"
            else:
                result_status = "needs_human_review"
            payload = dict(run.payload_json or {})
            payload.update(
                {
                    "completed_at": completed_at,
                    "status": "completed",
                    "result_status": result_status,
                    "summary": summary,
                }
            )
            run.completed_at = completed_at
            run.status = "completed"
            run.payload_json = payload
        finished = self.get_eval_run(eval_run_id)
        task_id = self._string((finished or {}).get("optimization_task_id"))
        if task_id and finished:
            next_status = "completed" if finished.get("result_status") == "passed" else str(finished.get("result_status") or "needs_human_review")
            self._attach_task_regression_run(task_id, finished, status=next_status)
            return self.get_eval_run(eval_run_id)
        return finished

    def fail_eval_run(self, eval_run_id: str, *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        error_json = {"error_code": error_code, "message": message, "created_at": utc_now(), "eval_run_id": eval_run_id}
        with self.Session.begin() as db:
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            payload = dict(run.payload_json or {})
            payload.update({"status": "failed", "result_status": "failed", "completed_at": utc_now(), "error_json": error_json})
            run.status = "failed"
            run.completed_at = payload["completed_at"]
            run.payload_json = payload
        failed = self.get_eval_run(eval_run_id)
        task_id = self._string((failed or {}).get("optimization_task_id"))
        if task_id and failed:
            self._attach_task_regression_run(task_id, failed, status="failed")
        return failed

    def list_eval_runs(
        self,
        *,
        optimization_task_id: Optional[str] = None,
        agent_version_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {"optimization_task_id": optimization_task_id, "agent_version_id": agent_version_id, "status": status}
        with self.Session() as db:
            runs = [self._eval_run_to_dict(row) for row in db.scalars(select(EvalRunModel).order_by(EvalRunModel.created_at.desc())).all()]
        return self._filter_records(runs, filters, limit)

    def get_eval_run(self, eval_run_id: str) -> Optional[dict[str, Any]]:
        if not eval_run_id:
            return None
        with self.Session() as db:
            row = db.get(EvalRunModel, eval_run_id)
            return self._eval_run_to_dict(row) if row else None

    def find_run(self, *, run_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        if not run_id:
            return None
        with self.Session() as db:
            row = db.get(AgentRunModel, run_id)
            return row.payload_json if row else None

    def find_run_for_event(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        exact = self.find_run(run_id=self._string(event.get("run_id")))
        if exact:
            return exact
        with self.Session() as db:
            runs = [row.payload_json for row in db.scalars(select(AgentRunModel).order_by(AgentRunModel.created_at.desc())).all()]
        session_id = self._string(event.get("session_id"))
        alert_id = self._string(event.get("alert_id"))
        case_id = self._string(event.get("case_id"))
        for run in runs:
            if session_id and run.get("session_id") == session_id and self._same_case_or_alert(run, alert_id, case_id):
                return run
        for run in runs:
            if self._same_case_or_alert(run, alert_id, case_id):
                return run
        return None

    def offline_attribution_output(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": job["feedback_case_id"],
            "attribution_job_id": job["job_id"],
            "status": "needs_human_review",
            "problem_type": "insufficient_information",
            "optimization_object_type": "not_actionable",
            "actionability": "needs_human_analysis",
            "confidence": "low",
            "human_review_required": True,
            "evidence_refs": [
                {
                    "type": "evidence_package",
                    "id": job["evidence_package_id"],
                    "reason": "证据包已固化；当前未配置模型提供商，需人工或归因分析智能体补充分析。",
                }
            ],
            "responsibility_boundary": {"owner": "needs_human_analysis", "reason": "未形成可安全转为主智能体 workspace 修改的归因结论。"},
            "rationale": "采集链路不再使用旧版规则归因；离线模式仅生成低置信、需人工复核的结构化占位结果。",
            "recommended_next_step": "needs_human_review",
        }

    def offline_proposal_output(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": job["feedback_case_id"],
            "proposal_job_id": job["job_id"],
            "status": "needs_human_review",
            "proposals": [],
            "external_guidance": [
                {
                    "owner": "needs_human_analysis",
                    "actionability": "needs_human_analysis",
                    "recommendation": "当前没有高置信归因输出，不能创建主智能体 workspace 修改方案。",
                    "reason": "归因 job 未给出 direct_workspace_change 或 workspace_config_change。",
                }
            ],
            "no_action_reason": "needs_human_analysis",
        }

    def offline_execution_plan_output(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": job["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "needs_human_review",
            "baseline_agent_version_id": job.get("baseline_agent_version_id"),
            "summary": "当前未配置模型提供商，系统不能自动生成受控 patch。",
            "operations": [],
            "validation": "人工按优化方案修改后，可继续使用人工标记已应用兜底流程。",
            "risk": "离线占位不会修改主智能体 workspace。",
            "human_review_required": True,
            "no_action_reason": "MODEL_PROVIDER_NOT_CONFIGURED",
        }

    def deterministic_execution_plan_output(self, job: dict[str, Any]) -> Optional[dict[str, Any]]:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        proposal = input_json.get("proposal") if isinstance(input_json.get("proposal"), dict) else {}
        target_paths = [str(path) for path in input_json.get("target_paths") or [] if isinstance(path, str)]
        if len(target_paths) != 1:
            return None
        target_path = target_paths[0]
        if not target_path.startswith("evals/") or proposal.get("target_type") != "eval_case":
            return None
        recommendation = self._string(proposal.get("recommendation")) or self._string(proposal.get("title")) or "反馈回归评估用例"
        content = {
            "schema_version": "feedback-eval-case/v1",
            "source": "execution_optimizer",
            "source_feedback_case_id": input_json.get("feedback_case_id"),
            "source_proposal_id": input_json.get("proposal_id"),
            "source_optimization_task_id": input_json.get("optimization_task_id"),
            "title": self._string(proposal.get("title")) or "反馈回归评估用例",
            "prompt": recommendation,
            "labels": self._unique_strings(["feedback_optimization", "eval_case", "execution_optimizer"]),
            "expected_behavior": self._string(proposal.get("expected_effect"))
            or self._string(proposal.get("validation"))
            or recommendation,
            "checks_json": {
                "requires_non_empty_answer": True,
                "requires_no_runtime_errors": True,
                "requires_human_review": True,
                "notes": "由 execution-optimizer 根据已批准优化方案生成的评估用例草案，首次应用后建议人工补充精确断言。",
            },
            "source_summary": {
                "recommendation": recommendation,
                "validation": proposal.get("validation"),
                "risk": proposal.get("risk"),
                "target_path": target_path,
            },
        }
        return {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": job["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": job.get("baseline_agent_version_id"),
            "summary": "根据已批准的评估用例建议生成受控 create_file 执行方案。",
            "operations": [
                {
                    "operation": "create_file",
                    "path": target_path,
                    "content": json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    "rationale": "目标为 evals/ 下的评估用例文件，可由后端根据方案确定性生成草案，避免执行优化智能体生成超长 JSON 超时。",
                }
            ],
            "validation": "应用前检查将创建的评估用例 JSON、目标路径和必需字段；应用后可在评估用例详情中补充精确断言，再手动运行回归验证。",
            "risk": "该方案只新增评估用例草案，不修改主智能体指令；语义断言仍需人工复核。",
            "human_review_required": True,
        }

    def _normalize_proposal_output(self, output: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        normalized = {**output, "proposals": [], "external_guidance": list(output.get("external_guidance") or [])}
        for item in output.get("proposals") or []:
            target_path = self._string(item.get("target_path"))
            actionability = self._string(item.get("actionability")) or "needs_human_analysis"
            if not target_path or not self._target_allowed(target_path):
                normalized["external_guidance"].append(
                    {
                        "owner": item.get("target_type") or "needs_human_analysis",
                        "actionability": "needs_human_analysis",
                        "recommendation": item.get("recommendation") or "建议目标路径不在允许范围内，需人工分析。",
                        "reason": "TARGET_PATH_NOT_ALLOWED",
                    }
                )
                continue
            normalized["proposals"].append(
                {
                    **item,
                    "proposal_id": item.get("proposal_id") or f"opp-{uuid.uuid4()}",
                    "created_at": utc_now(),
                    "feedback_case_id": job["feedback_case_id"],
                    "proposal_job_id": job["job_id"],
                    "status": "pending_review",
                    "actionability": actionability,
                    "base_agent_version_id": self._current_agent_version_id(),
                }
            )
        if not normalized["proposals"] and not normalized["external_guidance"]:
            normalized["no_action_reason"] = normalized.get("no_action_reason") or "NO_ACTIONABLE_PROPOSAL"
        return normalized

    def _upsert_external_governance_items(self, normalized: dict[str, Any], job: dict[str, Any]) -> list[dict[str, Any]]:
        guidance_items = [item for item in normalized.get("external_guidance") or [] if isinstance(item, dict)]
        if not guidance_items:
            return []
        with self.Session.begin() as db:
            existing_rows = db.scalars(
                select(ExternalGovernanceItemModel).where(ExternalGovernanceItemModel.proposal_job_id == job["job_id"])
            ).all()
            existing_by_index = {
                int((row.payload_json or {}).get("source_index")): row
                for row in existing_rows
                if isinstance((row.payload_json or {}).get("source_index"), int)
            }
            result: list[dict[str, Any]] = []
            for index, guidance in enumerate(guidance_items):
                now = utc_now()
                existing = existing_by_index.get(index)
                external_item_id = existing.external_item_id if existing else f"egi-{uuid.uuid4()}"
                payload = {
                    "schema_version": "external-governance-item/v1",
                    "external_item_id": external_item_id,
                    "created_at": existing.created_at if existing else now,
                    "updated_at": now,
                    "status": existing.status if existing else "pending_notification",
                    "feedback_case_id": job["feedback_case_id"],
                    "proposal_job_id": job["job_id"],
                    "source_index": index,
                    "owner": self._string(guidance.get("owner")) or "needs_human_analysis",
                    "actionability": self._string(guidance.get("actionability")) or "external_guidance",
                    "recommendation": self._string(guidance.get("recommendation")) or "",
                    "reason": self._string(guidance.get("reason")),
                    "latest_notification_id": existing.latest_notification_id if existing else None,
                }
                if existing:
                    existing.updated_at = now
                    existing.owner = payload["owner"]
                    existing.actionability = payload["actionability"]
                    existing.payload_json = {**(existing.payload_json or {}), **payload}
                else:
                    db.add(
                        ExternalGovernanceItemModel(
                            external_item_id=external_item_id,
                            created_at=payload["created_at"],
                            updated_at=payload["updated_at"],
                            status=payload["status"],
                            feedback_case_id=payload["feedback_case_id"],
                            proposal_job_id=payload["proposal_job_id"],
                            owner=payload["owner"],
                            actionability=payload["actionability"],
                            latest_notification_id=payload["latest_notification_id"],
                            payload_json=payload,
                        )
                    )
                result.append({**guidance, **payload})
            return result

    def _upsert_external_governance_item_for_plan_task(
        self,
        batch: dict[str, Any],
        plan: dict[str, Any],
        plan_task: dict[str, Any],
    ) -> dict[str, Any]:
        existing_id = self._string(plan_task.get("external_item_id"))
        existing = self.find_external_governance_item(existing_id) if existing_id else None
        now = utc_now()
        external_item_id = existing_id or f"egi-{uuid.uuid4()}"
        feedback_case_id = self._latest(plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids")) or ""
        proposal_job_id = f"batch-plan-task-{batch['batch_id']}-{plan_task['plan_task_id']}"
        payload = {
            "schema_version": "external-governance-item/v1",
            "external_item_id": external_item_id,
            "created_at": existing.get("created_at") if existing else now,
            "updated_at": now,
            "status": existing.get("status") if existing else "pending_notification",
            "feedback_case_id": feedback_case_id,
            "proposal_job_id": proposal_job_id,
            "source_index": int(plan_task.get("source_index") or 0),
            "owner": self._string(plan_task.get("owner")) or self._string(plan_task.get("target_type")) or "external_system",
            "actionability": self._string(plan_task.get("actionability")) or "external_guidance",
            "title": self._string(plan_task.get("title")) or "外部系统优化任务",
            "description": self._string(plan_task.get("description")) or "",
            "objective": self._string(plan_task.get("objective")) or "",
            "target_summary": self._string(plan_task.get("target_summary")) or "",
            "task_context": plan_task.get("task_context") if isinstance(plan_task.get("task_context"), dict) else {},
            "recommendation": self._string(plan_task.get("recommendation")) or "",
            "recommended_actions": self._string_list(plan_task.get("recommended_actions")),
            "acceptance_criteria": self._string_list(plan_task.get("acceptance_criteria")),
            "expected_effect": self._string(plan_task.get("expected_effect")) or "",
            "validation": self._string(plan_task.get("validation")) or "",
            "risk": self._string(plan_task.get("risk")) or "",
            "analysis_summary": self._string(plan_task.get("analysis_summary")) or "",
            "evidence_summary": self._string(plan_task.get("evidence_summary")) or "",
            "evidence_refs": [dict(ref) for ref in plan_task.get("evidence_refs") or [] if isinstance(ref, dict)],
            "reason": self._string(plan_task.get("reason")) or self._string(plan_task.get("rationale")),
            "latest_notification_id": existing.get("latest_notification_id") if existing else None,
            "latest_webhook_alias": existing.get("latest_webhook_alias") if existing else None,
            "latest_notification": existing.get("latest_notification") if existing else None,
            "source": "feedback_optimization_batch",
            "batch_id": batch.get("batch_id"),
            "optimization_plan_id": plan.get("optimization_plan_id"),
            "plan_task_id": plan_task.get("plan_task_id"),
            "target_type": plan_task.get("target_type"),
            "target_path": plan_task.get("target_path"),
            "feedback_case_ids": plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
            "eval_case_ids": batch.get("eval_case_ids") or [],
            "source_attribution_job_ids": plan_task.get("attribution_job_ids") or [],
        }
        with self.Session.begin() as db:
            row = db.get(ExternalGovernanceItemModel, external_item_id)
            if row:
                row.updated_at = now
                row.status = payload["status"]
                row.owner = payload["owner"]
                row.actionability = payload["actionability"]
                row.payload_json = {**(row.payload_json or {}), **payload}
            else:
                db.add(
                    ExternalGovernanceItemModel(
                        external_item_id=external_item_id,
                        created_at=payload["created_at"],
                        updated_at=payload["updated_at"],
                        status=payload["status"],
                        feedback_case_id=payload["feedback_case_id"],
                        proposal_job_id=payload["proposal_job_id"],
                        owner=payload["owner"],
                        actionability=payload["actionability"],
                        latest_notification_id=payload["latest_notification_id"],
                        payload_json=payload,
                    )
                )
        return self.find_external_governance_item(external_item_id) or payload

    def _normalize_source_kind(self, source_kind: str) -> str:
        normalized = str(source_kind or "").strip()
        aliases = {
            "feedback_signal": "signal",
            "event": "soc_event",
            "pending": "pending_correlation",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"signal", "soc_event", "pending_correlation"}:
            raise ValueError(f"Unsupported feedback source kind: {source_kind}")
        return normalized

    def _normalize_source_refs(self, source_refs: list[dict[str, Any]]) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in source_refs:
            if not isinstance(item, dict):
                continue
            try:
                kind = self._normalize_source_kind(str(item.get("source_kind") or item.get("kind") or ""))
            except ValueError:
                continue
            source_id = self._string(item.get("source_id") or item.get("id"))
            if not source_id:
                continue
            key = (kind, source_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append({"source_kind": kind, "source_id": source_id})
        return refs

    def _find_source_record(self, source_kind: str, source_id: str) -> Optional[dict[str, Any]]:
        kind = self._normalize_source_kind(source_kind)
        if kind == "signal":
            return self.find_signal(source_id)
        if kind == "soc_event":
            return self.find_event(source_id)
        return self.find_pending(source_id)

    def _source_annotation_id(self, source_kind: str, source_id: str) -> str:
        return f"{self._normalize_source_kind(source_kind)}:{source_id}"

    def _find_source_annotation(self, source_kind: str, source_id: str) -> Optional[dict[str, Any]]:
        with self.Session() as db:
            row = db.get(FeedbackSourceAnnotationModel, self._source_annotation_id(source_kind, source_id))
            return dict(row.payload_json or {}) if row else None

    def _source_annotations_by_key(self) -> dict[tuple[str, str], dict[str, Any]]:
        with self.Session() as db:
            rows = db.scalars(select(FeedbackSourceAnnotationModel)).all()
        return {(row.source_kind, row.source_id): dict(row.payload_json or {}) for row in rows}

    def _cases_by_source_id(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for feedback_case in self.list_cases(limit=1000):
            for source_id in feedback_case.get("source_ids") or []:
                if isinstance(source_id, str) and source_id and source_id not in result:
                    result[source_id] = feedback_case
        return result

    def _find_case_for_source_id(self, source_id: str) -> Optional[dict[str, Any]]:
        if not source_id:
            return None
        for feedback_case in self.list_cases(limit=1000):
            if source_id in (feedback_case.get("source_ids") or []):
                return feedback_case
        return None

    def _source_row(
        self,
        *,
        source_kind: str,
        source_id: str,
        raw: dict[str, Any],
        annotation: Optional[dict[str, Any]] = None,
        feedback_case: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        annotation = annotation or {}
        feedback_case_id = self._string((feedback_case or {}).get("feedback_case_id"))
        eval_case = self.find_eval_case(source_feedback_case_id=feedback_case_id) if feedback_case_id else None
        attribution_job_id = self._latest((feedback_case or {}).get("attribution_job_ids"))
        attribution_job = self.get_job(attribution_job_id) if attribution_job_id else None
        run_id = (
            self._string(raw.get("run_id"))
            or self._string(raw.get("matched_run_id"))
            or self._string(raw.get("resolved_run_id"))
        )
        labels = annotation.get("labels") if isinstance(annotation.get("labels"), list) else raw.get("labels")
        if not isinstance(labels, list):
            labels = [raw.get("event_type")] if raw.get("event_type") else []
        comment = self._string(annotation.get("comment")) or self._string(raw.get("comment"))
        created_at = self._string(raw.get("created_at")) or self._string(raw.get("timestamp")) or self._string(annotation.get("created_at"))
        updated_at = self._string(annotation.get("updated_at")) or self._string(raw.get("updated_at")) or created_at
        return {
            "schema_version": "feedback-source/v1",
            "source_kind": self._normalize_source_kind(source_kind),
            "source_id": source_id,
            "id": source_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "status": self._string(annotation.get("status")) or self._base_source_status(source_kind, raw),
            "label": self._source_label(source_kind, raw, labels, comment),
            "labels": self._unique_strings([str(item) for item in labels or [] if str(item).strip()]),
            "comment": comment,
            "priority": self._string(annotation.get("priority")) or "medium",
            "requires_review": bool(annotation.get("requires_review") if "requires_review" in annotation else raw.get("requires_review")),
            "metadata": annotation.get("metadata") if isinstance(annotation.get("metadata"), dict) else {},
            "run_id": run_id,
            "session_id": self._string(raw.get("session_id")),
            "alert_id": self._string(raw.get("alert_id")),
            "case_id": self._string(raw.get("case_id")),
            "feedback_case_id": feedback_case_id,
            "eval_case_id": self._string((eval_case or {}).get("eval_case_id")),
            "latest_attribution_job_id": attribution_job_id,
            "latest_attribution_status": self._string((attribution_job or {}).get("status")),
            "raw": raw,
        }

    def _base_source_status(self, source_kind: str, raw: dict[str, Any]) -> str:
        kind = self._normalize_source_kind(source_kind)
        if kind == "signal":
            return "needs_review" if raw.get("requires_review") else "collected"
        if kind == "soc_event":
            return "matched" if raw.get("matched_run_id") or raw.get("run_id") else "pending_correlation"
        return self._string(raw.get("status")) or "pending"

    def _source_label(self, source_kind: str, raw: dict[str, Any], labels: Any, comment: Optional[str]) -> str:
        if comment:
            return comment[:120]
        if isinstance(labels, list) and labels:
            return ", ".join(str(item) for item in labels[:3])
        if raw.get("event_type"):
            return str(raw["event_type"])
        if raw.get("source_type"):
            return str(raw["source_type"])
        return self._normalize_source_kind(source_kind)

    def _source_case_title(self, source: dict[str, Any]) -> str:
        return (
            self._string(source.get("comment"))
            or self._string(source.get("label"))
            or f"{source.get('source_kind') or 'feedback'} {source.get('source_id') or ''}"
        )[:120]

    def _build_eval_case_from_source(self, ref: dict[str, str], feedback_case: dict[str, Any]) -> Optional[dict[str, Any]]:
        source = self.find_feedback_source(ref["source_kind"], ref["source_id"])
        if not source:
            return None
        run_id = self._latest(feedback_case.get("run_ids")) or self._string(source.get("run_id"))
        source_run = self.find_run(run_id=run_id) if run_id else None
        prompt = (
            self._string((source_run or {}).get("message"))
            or self._string(source.get("comment"))
            or self._string(source.get("label"))
            or self._string(feedback_case.get("title"))
        )
        if not prompt:
            return None
        labels = self._unique_strings(
            [
                "feedback_optimization",
                str(source.get("source_kind") or ""),
                *[str(item) for item in source.get("labels") or []],
            ]
        )
        checks = {
            "requires_non_empty_answer": True,
            "requires_no_runtime_errors": True,
            "requires_tool_use": any(label in labels for label in ("tool_data_incomplete", "tool_data_quality", "tool_misuse", "evidence_gap")),
            "preferred_tools": ["Read", "Grep", "Glob"],
            "notes": "由反馈信息默认生成；开发人员可在反馈信息详情中逐条编辑输入、期望行为和检查规则。",
        }
        created_at = utc_now()
        expected_behavior = (
            f"复测“{feedback_case.get('title') or source.get('label') or ref['source_id']}”对应原始输入，"
            "回答应解决反馈备注指出的问题，并保持输出完整、可核查、无运行错误。"
        )
        return {
            "schema_version": "feedback-eval-case/v1",
            "eval_case_id": f"evc-{uuid.uuid4()}",
            "created_at": created_at,
            "updated_at": created_at,
            "status": "active",
            "source": "feedback_source_default",
            "source_feedback_case_id": feedback_case["feedback_case_id"],
            "source_run_id": run_id,
            "source_kind": ref["source_kind"],
            "source_id": ref["source_id"],
            "source_refs": [ref],
            "prompt": prompt,
            "labels": labels,
            "expected_behavior": expected_behavior,
            "checks_json": checks,
            "source_summary": {
                "feedback_title": feedback_case.get("title"),
                "source_label": source.get("label"),
                "comment": source.get("comment"),
                "original_answer_summary": (source_run or {}).get("answer_summary"),
            },
        }

    def _replace_eval_case_payload(self, payload: dict[str, Any]) -> None:
        with self.Session.begin() as db:
            row = db.get(EvalCaseModel, payload["eval_case_id"])
            if not row:
                return
            row.updated_at = payload["updated_at"]
            row.status = payload["status"]
            row.source_feedback_case_id = self._string(payload.get("source_feedback_case_id"))
            row.source_run_id = self._string(payload.get("source_run_id"))
            row.labels_json = list(payload.get("labels") or [])
            row.payload_json = payload

    def _batch_to_dict(self, row: FeedbackOptimizationBatchModel) -> dict[str, Any]:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "batch_id": row.batch_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "title": row.title,
            }
        )
        task_id = self._string(payload.get("optimization_task_id"))
        execution_job_id = self._string(payload.get("execution_job_id"))
        eval_run_id = self._string(payload.get("eval_run_id"))
        plan = payload.get("optimization_plan") if isinstance(payload.get("optimization_plan"), dict) else None
        if plan is not None:
            payload["optimization_plan"] = self._normalize_plan_task_collections(payload, plan)
        task = self.find_task(task_id) if task_id else None
        if task:
            payload["optimization_task"] = task
            latest_execution = task.get("latest_execution_job") if isinstance(task.get("latest_execution_job"), dict) else None
            if latest_execution:
                payload["execution_job"] = latest_execution
                payload["execution_job_id"] = latest_execution.get("execution_job_id")
            if not eval_run_id:
                task_status = self._string(task.get("status"))
                if task_status in {"execution_planning", "execution_ready", "execution_failed", "needs_human_review", "failed", "applied_pending_regression", "regression_running"}:
                    payload["status"] = task_status
        elif execution_job_id and not isinstance(payload.get("execution_job"), dict):
            payload["execution_job"] = self.get_execution_job(execution_job_id)
        if eval_run_id and not isinstance(payload.get("latest_eval_run"), dict):
            payload["latest_eval_run"] = self.get_eval_run(eval_run_id)
        return payload

    def _normalize_plan_task_collections(self, batch: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        raw_tasks = [dict(item) for item in plan.get("tasks") or [] if isinstance(item, dict)]
        executable_tasks = [
            self._normalize_plan_task(batch, plan, item)
            for item in raw_tasks
            if item.get("execution_kind") in {"workspace_execution", "external_webhook"}
        ]
        blocked_items = [self._normalize_blocked_item(batch, plan, dict(item)) for item in plan.get("blocked_items") or [] if isinstance(item, dict)]
        blocked_items.extend(self._blocked_items_from_tasks(batch, plan, raw_tasks))
        if not raw_tasks and not blocked_items:
            legacy_item = self._legacy_plan_task_or_blocked_item(batch, plan)
            if legacy_item.get("execution_kind") in {"workspace_execution", "external_webhook"}:
                executable_tasks.append(self._normalize_plan_task(batch, plan, legacy_item))
            else:
                blocked_items.append(self._normalize_blocked_item(batch, plan, legacy_item))
        return {
            **plan,
            "tasks": executable_tasks,
            "blocked_items": blocked_items,
            "task_summary": self._plan_task_summary(executable_tasks),
            "blocked_summary": {"total": len(blocked_items)},
        }

    def _normalize_plan_task(self, batch: dict[str, Any], plan: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        target_type = self._string(item.get("target_type")) or self._string(plan.get("target_type")) or "not_actionable"
        execution_kind = self._string(item.get("execution_kind")) or "workspace_execution"
        target_path = self._string(item.get("target_path")) or None
        owner = self._string(item.get("owner")) or self._external_owner_for_target(target_type) or target_type
        rationale = self._string(item.get("rationale")) or self._string(plan.get("rationale"))
        analysis_summary = self._string(item.get("analysis_summary")) or self._short_text(rationale, 420)
        evidence_refs = [dict(ref) for ref in item.get("evidence_refs") or [] if isinstance(ref, dict)]
        evidence_summary = self._string(item.get("evidence_summary")) or self._evidence_summary(evidence_refs)
        task_context = self._normalize_task_context(item.get("task_context"), rationale, owner)
        if execution_kind == "external_webhook":
            owner = self._external_owner_from_context(owner, task_context)
        target_summary = self._string(item.get("target_summary"))
        if execution_kind == "external_webhook" and (
            not target_summary
            or target_type in target_summary
            or "external-mcp-service" in target_summary
            or "对应外部系统" in target_summary
        ):
            target_summary = self._plan_task_target_summary(target_type, execution_kind, owner, target_path)
        normalized = {
            **item,
            "schema_version": "feedback-optimization-plan-task/v2",
            "plan_task_id": self._string(item.get("plan_task_id")) or f"fopt-{uuid.uuid4()}",
            "execution_kind": execution_kind,
            "owner": owner,
            "title": self._clean_plan_task_title(item.get("title"), target_type, execution_kind, int(item.get("source_index") or 0), task_context),
            "description": self._clean_plan_task_description(item.get("description"), target_type, execution_kind, owner, target_path, task_context),
            "objective": self._clean_plan_task_objective(item.get("objective"), target_type, execution_kind, task_context),
            "target_summary": target_summary,
            "recommended_actions": self._string_list(item.get("recommended_actions")) or self._plan_task_actions(target_type, execution_kind, target_path, owner),
            "acceptance_criteria": self._clean_plan_task_acceptance_criteria(item.get("acceptance_criteria"), execution_kind, target_path, task_context),
            "task_context": task_context,
            "analysis_summary": analysis_summary,
            "evidence_summary": evidence_summary,
            "evidence_refs": evidence_refs,
            "recommendation": self._clean_plan_task_recommendation(item.get("recommendation"), target_type, execution_kind),
            "feedback_case_ids": self._string_list(item.get("feedback_case_ids")) or self._string_list(batch.get("feedback_case_ids")),
            "eval_case_ids": self._string_list(item.get("eval_case_ids")) or self._string_list(batch.get("eval_case_ids")),
            "attribution_job_ids": self._string_list(item.get("attribution_job_ids")) or self._string_list(plan.get("attribution_job_ids")),
        }
        return normalized

    def _normalize_blocked_item(self, batch: dict[str, Any], plan: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        target_type = self._string(item.get("target_type")) or "not_actionable"
        evidence_refs = [dict(ref) for ref in item.get("evidence_refs") or [] if isinstance(ref, dict)]
        rationale = self._string(item.get("rationale")) or self._string(plan.get("rationale"))
        return {
            **item,
            "schema_version": "feedback-optimization-blocked-item/v1",
            "blocked_item_id": self._string(item.get("blocked_item_id")) or self._string(item.get("plan_task_id")) or f"fobi-{uuid.uuid4()}",
            "status": "blocked",
            "title": self._string(item.get("title")) or "未形成可执行优化任务",
            "target_type": target_type,
            "reason": self._string(item.get("reason")) or rationale or "该项不能自动执行，也没有可通知的外部目标。",
            "analysis_summary": self._string(item.get("analysis_summary")) or self._short_text(rationale, 420),
            "evidence_summary": self._string(item.get("evidence_summary")) or self._evidence_summary(evidence_refs),
            "feedback_case_ids": self._string_list(item.get("feedback_case_ids")) or self._string_list(batch.get("feedback_case_ids")),
            "eval_case_ids": self._string_list(item.get("eval_case_ids")) or self._string_list(batch.get("eval_case_ids")),
            "attribution_job_ids": self._string_list(item.get("attribution_job_ids")) or self._string_list(plan.get("attribution_job_ids")),
        }

    def _blocked_items_from_tasks(self, batch: dict[str, Any], plan: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocked: list[dict[str, Any]] = []
        for item in tasks:
            if item.get("execution_kind") in {"workspace_execution", "external_webhook"}:
                continue
            blocked.append(self._normalize_blocked_item(batch, plan, item))
        return blocked

    def _legacy_plan_task_or_blocked_item(self, batch: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        target_type = self._string(plan.get("target_type") or plan.get("optimization_object_type")) or "not_actionable"
        target_path = self._string(plan.get("target_path")) or None
        actionability = self._string(plan.get("actionability")) or "needs_human_analysis"
        execution_kind = "blocked"
        status = "blocked"
        reason = self._string(plan.get("no_action_reason")) or self._string(plan.get("recommendation"))
        if actionability in {"direct_workspace_change", "workspace_config_change", "eval_only"} and target_path and self._target_allowed(target_path):
            execution_kind = "workspace_execution"
            status = "pending_execution"
            reason = None
        elif actionability == "external_guidance" or target_type in {"external_mcp_service", "soc_process", "mcp_description"}:
            execution_kind = "external_webhook"
            status = "pending_notification"
            reason = None
        item_id = f"fopt-legacy-{self._string(plan.get('optimization_plan_id')) or self._string(batch.get('batch_id'))}"
        item = {
            "schema_version": "feedback-optimization-plan-task/v1",
            "plan_task_id": item_id,
            "source_index": 0,
            "execution_kind": execution_kind,
            "status": status,
            "title": plan.get("title") or "历史优化方案任务",
            "target_type": target_type,
            "target_path": target_path,
            "owner": self._external_owner_for_target(target_type) or target_type,
            "actionability": actionability,
            "confidence": plan.get("confidence"),
            "problem_type": self._latest(plan.get("problem_types") or []),
            "recommendation": plan.get("recommendation") or "",
            "expected_effect": plan.get("expected_effect") or "",
            "validation": plan.get("validation") or "",
            "risk": plan.get("risk") or "",
            "rationale": plan.get("rationale") or "",
            "reason": reason,
            "feedback_case_ids": plan.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
            "eval_case_ids": plan.get("eval_case_ids") or batch.get("eval_case_ids") or [],
            "attribution_job_ids": plan.get("attribution_job_ids") or [],
            "created_at": plan.get("created_at") or batch.get("created_at"),
        }
        if execution_kind == "blocked":
            item.update(
                {
                    "schema_version": "feedback-optimization-blocked-item/v1",
                    "blocked_item_id": item_id,
                    "reason": reason or "历史方案未形成可执行任务。",
                }
            )
        return item

    def _update_batch(self, batch_id: str, *, status: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        now = utc_now()
        with self.Session.begin() as db:
            row = db.get(FeedbackOptimizationBatchModel, batch_id)
            if not row:
                return None
            payload = dict(row.payload_json or {})
            payload.update(fields)
            payload["status"] = status
            payload["updated_at"] = now
            row.status = status
            row.updated_at = now
            row.title = self._string(payload.get("title")) or row.title
            row.payload_json = payload
        return self.find_optimization_batch(batch_id)

    def _batch_plan_task(self, batch_id: str, plan_task_id: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        batch = self.find_optimization_batch(batch_id)
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        if not batch or not plan:
            return None, None, None
        task = self._plan_task_from_batch(batch, plan_task_id)
        return batch, plan, task

    def _plan_task_from_batch(self, batch: Optional[dict[str, Any]], plan_task_id: str) -> Optional[dict[str, Any]]:
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        for task in (plan or {}).get("tasks") or []:
            if isinstance(task, dict) and self._string(task.get("plan_task_id")) == plan_task_id:
                return dict(task)
        return None

    def _update_batch_plan_task(
        self,
        batch_id: str,
        plan_task_id: str,
        updates: dict[str, Any],
        *,
        batch_status: Optional[str] = None,
        top_level_fields: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        batch = self.find_optimization_batch(batch_id)
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        if not batch or not plan:
            return None
        tasks = [dict(item) for item in plan.get("tasks") or [] if isinstance(item, dict)]
        changed = False
        now = utc_now()
        for index, task in enumerate(tasks):
            if self._string(task.get("plan_task_id")) == plan_task_id:
                tasks[index] = {**task, **updates, "updated_at": now}
                changed = True
                break
        if not changed:
            return None
        next_plan = {**plan, "tasks": tasks, "updated_at": now, "task_summary": self._plan_task_summary(tasks)}
        fields = {"optimization_plan": next_plan, **(top_level_fields or {})}
        return self._update_batch(batch_id, status=batch_status or str(batch.get("status") or "pending_approval"), fields=fields)

    def _plan_task_summary(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {"total": len(tasks), "workspace_execution": 0, "external_webhook": 0}
        for task in tasks:
            kind = self._string(task.get("execution_kind"))
            if kind not in {"workspace_execution", "external_webhook"}:
                continue
            summary[kind] = int(summary.get(kind) or 0) + 1
        return summary

    def _batch_attribution_outputs(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        job_ids = self._unique_strings(batch.get("attribution_job_ids") or [])
        if not job_ids:
            for feedback_case_id in batch.get("feedback_case_ids") or []:
                feedback_case = self.find_case(str(feedback_case_id))
                job_id = self._latest((feedback_case or {}).get("attribution_job_ids"))
                if job_id:
                    job_ids.append(job_id)
        outputs: list[dict[str, Any]] = []
        for job_id in job_ids:
            output = self.get_job_output(job_id, "attribution")
            if output:
                outputs.append({**output, "_job_id": job_id})
        return outputs

    def _assert_batch_plan_can_regenerate(self, batch: dict[str, Any]) -> None:
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        plan_status = self._string((plan or {}).get("status"))
        if (
            plan_status == "approved"
            or batch.get("optimization_task_id")
            or batch.get("execution_job_id")
            or batch.get("execution_apply_result")
        ):
            raise ValueError("当前优化方案已执行或进入执行链路，不能原地重新生成；请基于反馈信息创建新批次。")

    def _non_actionable_plan(self, batch: dict[str, Any], reason: str, regeneration_instruction: Optional[str] = None) -> dict[str, Any]:
        blocked_items = [
            {
                "schema_version": "feedback-optimization-blocked-item/v1",
                "blocked_item_id": f"fobi-{uuid.uuid4()}",
                "source_index": 0,
                "status": "blocked",
                "title": "未形成可执行优化任务",
                "target_type": "not_actionable",
                "target_path": None,
                "owner": "developer",
                "actionability": "needs_human_analysis",
                "recommendation": reason,
                "reason": reason,
                "feedback_case_ids": batch.get("feedback_case_ids") or [],
                "eval_case_ids": batch.get("eval_case_ids") or [],
                "attribution_job_ids": [],
                "created_at": utc_now(),
            }
        ]
        plan = {
            "schema_version": "feedback-optimization-plan/v1",
            "optimization_plan_id": f"fop-{uuid.uuid4()}",
            "batch_id": batch.get("batch_id"),
            "created_at": utc_now(),
            "status": "needs_human_review",
            "title": "不能生成可执行优化方案",
            "actionability": "needs_human_analysis",
            "target_type": "not_actionable",
            "target_path": None,
            "recommendation": reason,
            "expected_effect": "-",
            "validation": "-",
            "risk": "-",
            "source_refs": batch.get("source_refs") or [],
            "attribution_summaries": [],
            "tasks": [],
            "blocked_items": blocked_items,
            "task_summary": self._plan_task_summary([]),
            "blocked_summary": {"total": len(blocked_items)},
        }
        instruction = self._string(regeneration_instruction)
        if instruction:
            plan["regeneration_instruction"] = instruction
        return plan

    def _normalize_batch_plan_output(self, validated: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        batch_id = self._string(validated.get("batch_id")) or self._string(input_json.get("batch_id")) or ""
        batch = self.find_optimization_batch(batch_id) or {}
        plan = {
            **validated,
            "schema_version": "feedback-optimization-plan/v1",
            "source_output_schema_version": validated.get("schema_version"),
            "optimization_plan_id": self._string(validated.get("optimization_plan_id")) or f"fop-{uuid.uuid4()}",
            "batch_id": batch_id,
            "created_at": self._string(validated.get("created_at")) or utc_now(),
            "status": self._string(validated.get("status")) or "needs_human_review",
            "title": self._string(validated.get("title")) or "反馈批次优化方案",
            "recommendation": self._string(validated.get("recommendation")) or self._string(validated.get("summary")) or "根据归因结果生成优化任务。",
            "expected_effect": self._string(validated.get("expected_effect")) or "降低同类反馈再次出现的概率。",
            "validation": self._string(validated.get("validation")) or "使用本批次关联回归测试用例验证优化效果。",
            "risk": self._string(validated.get("risk")) or "需要关注优化后是否引入新的行为退化。",
            "source_refs": validated.get("source_refs") or batch.get("source_refs") or [],
            "feedback_case_ids": self._string_list(validated.get("feedback_case_ids")) or self._string_list(batch.get("feedback_case_ids")),
            "eval_case_ids": self._string_list(validated.get("eval_case_ids")) or self._string_list(batch.get("eval_case_ids")),
            "attribution_job_ids": self._string_list(validated.get("attribution_job_ids")) or self._string_list(input_json.get("attribution_job_ids")),
            "attribution_summaries": validated.get("attribution_summaries") or self._batch_plan_attribution_summaries(input_json.get("attribution_outputs")),
            "optimization_plan_job_id": job["job_id"],
            "generated_by": "proposal-generator",
        }
        if input_json.get("regeneration_instruction") and not plan.get("regeneration_instruction"):
            plan["regeneration_instruction"] = input_json["regeneration_instruction"]
        return self._normalize_plan_task_collections(batch or plan, plan)

    def _batch_plan_attribution_summaries(self, attributions: Any) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for item in attributions or []:
            if not isinstance(item, dict):
                continue
            summaries.append(
                {
                    "attribution_job_id": item.get("_job_id") or item.get("attribution_job_id"),
                    "feedback_case_id": item.get("feedback_case_id"),
                    "problem_type": item.get("problem_type"),
                    "optimization_object_type": item.get("optimization_object_type"),
                    "actionability": item.get("actionability"),
                    "confidence": item.get("confidence"),
                    "rationale": item.get("rationale"),
                }
            )
        return summaries

    def _build_batch_optimization_plan(
        self,
        batch: dict[str, Any],
        attributions: list[dict[str, Any]],
        *,
        regeneration_instruction: Optional[str] = None,
    ) -> dict[str, Any]:
        task_candidates = [self._build_batch_plan_task_or_blocked_item(batch, item, index) for index, item in enumerate(attributions)]
        tasks = [item for item in task_candidates if item.get("execution_kind") in {"workspace_execution", "external_webhook"}]
        blocked_items = [item for item in task_candidates if item.get("execution_kind") not in {"workspace_execution", "external_webhook"}]
        executable_tasks = tasks
        workspace_tasks = [item for item in tasks if item.get("execution_kind") == "workspace_execution"]
        primary = workspace_tasks[0] if workspace_tasks else executable_tasks[0] if executable_tasks else blocked_items[0]
        target_type = self._string(primary.get("target_type") or primary.get("optimization_object_type")) or "main_agent_claude_md"
        target_path = self._string(primary.get("target_path")) or self._plan_target_path(target_type)
        problem_types = self._unique_strings([self._string(item.get("problem_type")) or "" for item in attributions])
        confidence_values = self._unique_strings([self._string(item.get("confidence")) or "" for item in attributions])
        status = "pending_approval" if executable_tasks else "needs_human_review"
        actionability = self._string(primary.get("actionability")) or "needs_human_analysis"
        rationale_lines = [self._string(item.get("rationale")) for item in attributions if self._string(item.get("rationale"))]
        evidence_refs = [
            ref
            for item in attributions
            for ref in (item.get("evidence_refs") or [])
            if isinstance(ref, dict)
        ][:20]
        eval_case_ids = batch.get("eval_case_ids") or []
        recommendation = (
            "根据本批次归因结果，调整目标 Agent 配置，要求其在回答反馈暴露的场景时使用当前工作区的权威配置、"
            "工具调用和运行证据进行核查，避免依赖过期上下文或记忆回答。"
        )
        rationale = "\n\n".join(rationale_lines) or "归因结果未提供详细 rationale。"
        instruction = self._string(regeneration_instruction)
        if instruction:
            recommendation = f"{recommendation}\n\n开发人员补充要求：{instruction}"
            rationale = f"{rationale}\n\n生成补充要求：{instruction}"
        plan = {
            "schema_version": "feedback-optimization-plan/v1",
            "optimization_plan_id": f"fop-{uuid.uuid4()}",
            "batch_id": batch.get("batch_id"),
            "created_at": utc_now(),
            "status": status,
            "title": f"统筹 {len(batch.get('feedback_case_ids') or [])} 条反馈优化 {target_type}",
            "problem_types": problem_types,
            "confidence": "high" if "high" in confidence_values else "medium" if "medium" in confidence_values else "low",
            "actionability": actionability,
            "optimization_object_type": primary.get("target_type") or target_type,
            "target_type": primary.get("target_type") or target_type,
            "target_path": target_path,
            "recommendation": recommendation,
            "expected_effect": "提高反馈场景回答的完整性、可核查性和稳定性，降低同类反馈再次出现的概率。",
            "validation": (
                f"使用本批次关联的 {len(eval_case_ids)} 条回归测试用例逐条验证；所有用例均展示完整运行过程，"
                "失败项进入继续优化。"
            ),
            "risk": "可能增加回答前的工具调用成本；如指令过强，简单问题可能产生不必要的检查步骤。",
            "source_refs": batch.get("source_refs") or [],
            "feedback_case_ids": batch.get("feedback_case_ids") or [],
            "eval_case_ids": eval_case_ids,
            "attribution_job_ids": self._unique_strings([self._string(item.get("_job_id")) or "" for item in attributions]),
            "attribution_summaries": [
                {
                    "attribution_job_id": item.get("_job_id"),
                    "feedback_case_id": item.get("feedback_case_id"),
                    "problem_type": item.get("problem_type"),
                    "optimization_object_type": item.get("optimization_object_type"),
                    "actionability": item.get("actionability"),
                    "confidence": item.get("confidence"),
                    "rationale": item.get("rationale"),
                }
                for item in attributions
            ],
            "rationale": rationale,
            "evidence_refs": evidence_refs,
            "tasks": tasks,
            "task_summary": self._plan_task_summary(tasks),
            "blocked_items": blocked_items,
            "blocked_summary": {"total": len(blocked_items)},
        }
        if instruction:
            plan["regeneration_instruction"] = instruction
        return plan

    def _build_batch_plan_task_or_blocked_item(self, batch: dict[str, Any], attribution: dict[str, Any], index: int) -> dict[str, Any]:
        target_type = self._string(attribution.get("optimization_object_type")) or "not_actionable"
        actionability = self._string(attribution.get("actionability")) or "needs_human_analysis"
        target_path = self._plan_target_path(target_type)
        boundary = attribution.get("responsibility_boundary") if isinstance(attribution.get("responsibility_boundary"), dict) else {}
        owner = self._string(boundary.get("owner")) or self._external_owner_for_target(target_type) or target_type
        rationale = self._string(attribution.get("rationale"))
        attribution_job_id = self._string(attribution.get("_job_id") or attribution.get("attribution_job_id"))
        feedback_case_id = self._string(attribution.get("feedback_case_id"))
        evidence_refs = [dict(ref) for ref in attribution.get("evidence_refs") or [] if isinstance(ref, dict)]
        task_context = self._task_context_from_attribution(batch, attribution, evidence_refs, owner)
        execution_kind = "blocked"
        status = "blocked"
        next_step = self._string(attribution.get("recommended_next_step"))
        reason = None if next_step in {"generate_proposal", "needs_human_review", "stop"} else next_step
        if actionability in {"direct_workspace_change", "workspace_config_change", "eval_only"} and target_path and self._target_allowed(target_path):
            execution_kind = "workspace_execution"
            status = "pending_execution"
            reason = None
        elif actionability == "external_guidance" or target_type in {"external_mcp_service", "soc_process", "mcp_description"}:
            if self._task_context_is_actionable_external(task_context):
                execution_kind = "external_webhook"
                status = "pending_notification"
                reason = None
            else:
                reason = "当前证据不足以定位具体外部对象、接口或问题 ID，不能生成可执行外部优化任务；请补充 trace 或重新归因。"
        elif not target_path:
            reason = reason or "归因结果未指向可由当前 workspace 受控修改的目标文件。"
        if execution_kind == "external_webhook":
            owner = self._external_owner_from_context(owner, task_context)
        title = self._plan_task_title(target_type, execution_kind, index, task_context)
        recommendation = self._plan_task_recommendation(target_type, execution_kind)
        analysis_summary = self._short_text(rationale, 420)
        evidence_summary = self._evidence_summary(evidence_refs)
        item = {
            "schema_version": "feedback-optimization-plan-task/v2",
            "plan_task_id": f"fopt-{uuid.uuid4()}",
            "source_index": index,
            "execution_kind": execution_kind,
            "status": status,
            "title": title,
            "description": self._plan_task_description(target_type, execution_kind, owner, target_path, task_context),
            "objective": self._plan_task_objective(target_type, execution_kind, task_context),
            "target_summary": self._plan_task_target_summary(target_type, execution_kind, owner, target_path),
            "task_context": task_context,
            "target_type": target_type,
            "target_path": target_path,
            "owner": owner,
            "actionability": actionability,
            "confidence": attribution.get("confidence"),
            "problem_type": attribution.get("problem_type"),
            "recommendation": recommendation,
            "recommended_actions": self._plan_task_actions(target_type, execution_kind, target_path, owner),
            "acceptance_criteria": self._plan_task_acceptance_criteria(execution_kind, target_path, task_context),
            "expected_effect": "降低同类反馈再次出现的概率。",
            "validation": "使用本批次关联回归用例验证优化效果。",
            "risk": "需要关注优化后是否引入额外工具调用成本或外部系统变更风险。",
            "analysis_summary": analysis_summary,
            "evidence_summary": evidence_summary,
            "evidence_refs": evidence_refs,
            "rationale": rationale,
            "reason": reason,
            "feedback_case_ids": [feedback_case_id] if feedback_case_id else batch.get("feedback_case_ids") or [],
            "eval_case_ids": batch.get("eval_case_ids") or [],
            "attribution_job_ids": [attribution_job_id] if attribution_job_id else [],
            "created_at": utc_now(),
        }
        if execution_kind == "blocked":
            item.update(
                {
                    "schema_version": "feedback-optimization-blocked-item/v1",
                    "blocked_item_id": f"fobi-{uuid.uuid4()}",
                    "reason": reason or "归因结果未形成可执行 workspace 任务或外部 webhook 任务。",
                }
            )
        return item

    def _plan_task_title(self, target_type: str, execution_kind: str, index: int, task_context: Optional[dict[str, Any]] = None) -> str:
        if execution_kind == "workspace_execution":
            label = {
                "main_agent_claude_md": "优化 Agent 工作区指令",
                "instruction_gap": "补充 Agent 行为约束",
                "skill_gap": "优化 Agent 技能说明",
                "mcp_config": "优化 MCP 配置",
                "eval_case": "补充回归测试用例",
            }.get(target_type, "优化 Agent 工作区配置")
            return f"任务 {index + 1}: {label}"
        if execution_kind == "external_webhook":
            context_title = self._external_task_title_from_context(task_context or {})
            if context_title:
                return f"任务 {index + 1}: {context_title}"
            label = {
                "external_mcp_service": "补齐 MCP 服务返回数据能力",
                "mcp_description": "完善 MCP 服务描述",
                "soc_process": "优化 SOC 处置流程",
            }.get(target_type, "处理外部系统优化事项")
            return f"任务 {index + 1}: {label}"
        return f"阻塞项 {index + 1}: 未形成可执行优化任务"

    def _plan_task_description(
        self,
        target_type: str,
        execution_kind: str,
        owner: str,
        target_path: Optional[str],
        task_context: Optional[dict[str, Any]] = None,
    ) -> str:
        if execution_kind == "workspace_execution":
            return "根据反馈归因结果，调整受管 workspace 中的 Agent 配置、指令或用例，让 Agent 在同类场景中按当前证据和配置作答。"
        if execution_kind == "external_webhook":
            context_description = self._external_task_description_from_context(task_context or {})
            if context_description:
                return context_description
            owner_label = owner if owner and owner != target_type else "对应外部系统"
            return f"将反馈暴露的问题整理为外部系统优化任务，派发给 {owner_label} 处理。"
        return "该项没有形成可执行 workspace 任务或明确的外部系统派发目标。"

    def _plan_task_objective(self, target_type: str, execution_kind: str, task_context: Optional[dict[str, Any]] = None) -> str:
        if execution_kind == "workspace_execution":
            return "通过修改 workspace 受管配置或指令，降低同类反馈再次出现的概率。"
        if execution_kind == "external_webhook":
            context_objective = self._external_task_objective_from_context(task_context or {})
            if context_objective:
                return context_objective
            return "推动对应外部系统补齐能力、数据或流程，使 Agent 后续可获得可靠输入。"
        return "补充更多上下文后重新归因或重新生成优化方案。"

    def _plan_task_target_summary(self, target_type: str, execution_kind: str, owner: str, target_path: Optional[str]) -> str:
        if execution_kind == "workspace_execution":
            return f"workspace:{target_path or target_type}"
        if execution_kind == "external_webhook":
            return f"external:{owner or target_type}"
        return f"blocked:{target_type}"

    def _plan_task_actions(self, target_type: str, execution_kind: str, target_path: Optional[str], owner: str) -> list[str]:
        if execution_kind == "workspace_execution":
            return [
                f"由 execution-optimizer 读取已审批任务并生成针对 {target_path or target_type} 的受控执行方案。",
                "后端校验执行方案的目标路径、操作类型和文件哈希后应用变更。",
                "应用成功后创建 Agent 新版本，并使用本批次回归用例验证效果。",
            ]
        if execution_kind == "external_webhook":
            return [
                f"通过已配置 Webhook 将任务派发给 {owner or target_type}。",
                "Webhook payload 携带反馈、归因、测试用例、验收标准和证据摘要。",
                "本阶段以通知成功作为派发完成状态，不等待外部系统回调完成。",
            ]
        return ["重新补充反馈上下文后运行归因，或调整优化方案生成提示。"]

    def _plan_task_acceptance_criteria(
        self,
        execution_kind: str,
        target_path: Optional[str],
        task_context: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        if execution_kind == "workspace_execution":
            return [
                "关联回归测试用例通过，反馈指出的同类问题不再复现。",
                "Agent 在同类场景中能按优化目标使用当前配置、工具或数据完成回答。",
                "优化后回答满足反馈用例的期望行为，且不引入新的明显退化。",
            ]
        if execution_kind == "external_webhook":
            context_criteria = self._external_acceptance_criteria_from_context(task_context or {})
            if context_criteria:
                return context_criteria
            return [
                "外部系统在同类请求中提供 Agent 完成任务所需的完整、可靠输入。",
                "Agent 使用外部系统返回结果后，能完整回答反馈指出的缺失或错误内容。",
                "关联回归测试用例通过，反馈指出的问题不再复现。",
            ]
        return ["阻塞原因清晰可见，开发人员可据此重新归因或重新生成优化方案。"]

    def _clean_plan_task_title(
        self,
        value: Any,
        target_type: str,
        execution_kind: str,
        index: int,
        task_context: Optional[dict[str, Any]] = None,
    ) -> str:
        title = self._string(value)
        generic_fragments = ("补齐 MCP 服务返回数据能力", "外部系统优化", "external_mcp_service", "对应外部系统")
        if not title or target_type in title or any(fragment in title for fragment in generic_fragments):
            return self._plan_task_title(target_type, execution_kind, index, task_context)
        return title

    def _clean_plan_task_description(
        self,
        value: Any,
        target_type: str,
        execution_kind: str,
        owner: str,
        target_path: Optional[str],
        task_context: Optional[dict[str, Any]] = None,
    ) -> str:
        description = self._string(value)
        generic_fragments = ("对应外部系统", "外部系统优化任务", "external_mcp_service")
        text_quality_markers = ("时 返回", "时 时", "时 的")
        if (
            not description
            or target_type in description
            or any(fragment in description for fragment in generic_fragments)
            or any(marker in description for marker in text_quality_markers)
        ):
            return self._plan_task_description(target_type, execution_kind, owner, target_path, task_context)
        return description

    def _clean_plan_task_objective(
        self,
        value: Any,
        target_type: str,
        execution_kind: str,
        task_context: Optional[dict[str, Any]] = None,
    ) -> str:
        objective = self._string(value)
        generic_fragments = ("对应外部系统", "external_mcp_service")
        if not objective or target_type in objective or any(fragment in objective for fragment in generic_fragments):
            return self._plan_task_objective(target_type, execution_kind, task_context)
        return objective

    def _clean_plan_task_acceptance_criteria(
        self,
        value: Any,
        execution_kind: str,
        target_path: Optional[str],
        task_context: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        criteria = self._string_list(value)
        process_markers = ("Webhook", "2xx", "payload", "notification_failed", "execution-optimizer", "版本快照", "目标文件", "派发")
        generic_markers = ("外部系统在同类请求中", "完整、可靠输入")
        text_quality_markers = ("时 时", "时 的", "时 返回")
        if not criteria or any(any(marker in item for marker in (*process_markers, *generic_markers, *text_quality_markers)) for item in criteria):
            return self._plan_task_acceptance_criteria(execution_kind, target_path, task_context)
        return criteria

    def _task_context_from_attribution(
        self,
        batch: dict[str, Any],
        attribution: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        owner: str,
    ) -> dict[str, Any]:
        feedback_case_id = self._string(attribution.get("feedback_case_id"))
        feedback_case = self.find_case(feedback_case_id) if feedback_case_id else None
        text_parts = [
            self._string(attribution.get("rationale")) or "",
            self._string(attribution.get("recommended_next_step")) or "",
            self._string((attribution.get("responsibility_boundary") or {}).get("reason")) if isinstance(attribution.get("responsibility_boundary"), dict) else "",
            " ".join(self._string(ref.get("reason")) or "" for ref in evidence_refs),
        ]
        text = "\n".join(part for part in text_parts if part)
        tool_matches = list(re.finditer(r"\bmcp__([A-Za-z0-9_-]+)__([A-Za-z0-9_]+)__([A-Za-z0-9_]+)\b", text))
        tool_names = self._unique_strings(match.group(0) for match in tool_matches)
        mcp_server = tool_matches[0].group(1) if tool_matches else self._server_from_owner(owner)
        tool_operation = tool_matches[0].group(3) if tool_matches else ""
        api_info = self._api_info_from_tool_operation(tool_operation)
        alert_ids = self._unique_strings(
            [
                *(re.findall(r"\balert[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE)),
                *self._string_list((feedback_case or {}).get("alert_ids")),
            ]
        )
        case_ids = self._unique_strings(
            [
                *(re.findall(r"\bcase[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE)),
                *self._string_list((feedback_case or {}).get("case_ids")),
            ]
        )
        asset_ids = self._unique_strings(re.findall(r"\basset[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE))
        seed_values = self._unique_strings(f"seed={value}" for value in re.findall(r"\bseed\s*[=:]\s*([A-Za-z0-9_-]+)", text, flags=re.IGNORECASE))
        query_ids = self._unique_strings([*alert_ids, *case_ids, *asset_ids, *seed_values])
        dates = self._unique_strings(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text))
        affected_fields = self._affected_fields_from_text(text)
        observed_issue = self._observed_issue_from_text(text)
        context = {
            "mcp_server": mcp_server,
            "tool_name": tool_names[0] if tool_names else "",
            "tool_names": tool_names,
            "api_name": api_info.get("api_name") or "",
            "api_path": api_info.get("api_path") or "",
            "api_method": api_info.get("api_method") or "",
            "endpoint": api_info.get("endpoint") or "",
            "query_ids": query_ids,
            "alert_ids": alert_ids,
            "case_ids": case_ids,
            "asset_ids": asset_ids,
            "dates": dates,
            "affected_fields": affected_fields,
            "observed_issue": observed_issue,
            "expected_fix": self._expected_fix_from_context(mcp_server, api_info, query_ids, affected_fields, observed_issue),
        }
        return {key: value for key, value in context.items() if value not in ("", [], None)}

    def _normalize_task_context(self, value: Any, rationale: Optional[str], owner: str) -> dict[str, Any]:
        if isinstance(value, dict) and value:
            cleaned = {key: item for key, item in value.items() if item not in ("", [], None)}
            if isinstance(cleaned.get("expected_fix"), str):
                cleaned["expected_fix"] = (
                    cleaned["expected_fix"]
                    .replace("时 的", "时的")
                    .replace("时 返回", "时返回")
                    .replace("时 时", "时")
                )
            return cleaned
        attribution = {"rationale": rationale or "", "responsibility_boundary": {"owner": owner}}
        return self._task_context_from_attribution({}, attribution, [], owner)

    def _task_context_specificity(self, context: dict[str, Any]) -> int:
        categories = [
            bool(context.get("mcp_server")),
            bool(context.get("tool_name") or context.get("api_name") or context.get("api_path")),
            bool(context.get("query_ids") or context.get("dates")),
            bool(context.get("observed_issue")),
            bool(context.get("affected_fields")),
        ]
        return sum(1 for item in categories if item)

    def _task_context_is_actionable_external(self, context: dict[str, Any]) -> bool:
        has_interface = bool(context.get("tool_name") or context.get("api_name") or context.get("api_path"))
        return has_interface and self._task_context_specificity(context) >= 2

    def _external_owner_from_context(self, owner: str, context: dict[str, Any]) -> str:
        server = self._string(context.get("mcp_server"))
        generic_owners = {
            "",
            "external_mcp_service",
            "external-mcp-service",
            "mcp_description",
            "mcp-owner",
            "soc_process",
            "soc-process",
            "external_system",
            "external-system",
        }
        if server and owner in generic_owners:
            return server
        return owner

    def _server_from_owner(self, owner: str) -> str:
        if owner and owner not in {"external_mcp_service", "mcp_description", "soc_process", "external_system"}:
            return owner
        return ""

    def _api_info_from_tool_operation(self, operation: str) -> dict[str, str]:
        if not operation:
            return {}
        api_name = operation.split("_api_", 1)[0] if "_api_" in operation else operation
        result = {"api_name": api_name}
        if "_api_" not in operation:
            return result
        rest = operation.split("_api_", 1)[1]
        parts = [part for part in rest.split("_") if part]
        method = parts[-1].upper() if parts and parts[-1].lower() in {"get", "post", "put", "patch", "delete"} else ""
        path_parts = parts[:-1] if method else parts
        if path_parts:
            api_path = f"/api/{'/'.join(path_parts)}"
            result["api_path"] = api_path
            if method:
                result["api_method"] = method
                result["endpoint"] = f"{method} {api_path}"
        return result

    def _affected_fields_from_text(self, text: str) -> list[str]:
        candidates = [
            "event_time",
            "timestamp",
            "severity",
            "source",
            "status",
            "title",
            "asset_id",
            "alert_id",
            "case_id",
            "hostname",
            "ip",
            "process",
            "technique",
            "tactic",
        ]
        return [field for field in candidates if field in text]

    def _observed_issue_from_text(self, text: str) -> str:
        if not text:
            return ""
        fragments = re.split(r"(?<=[。！？；;])|\n+", text)
        keywords = ("缺失", "不足", "不完整", "固定", "不匹配", "不支持", "无法", "相距", "event_time", "时间戳", "字段")
        for fragment in fragments:
            clean = self._short_text(fragment, 260)
            if clean and any(keyword in clean for keyword in keywords):
                return clean
        return self._short_text(text, 260)

    def _expected_fix_from_context(
        self,
        mcp_server: str,
        api_info: dict[str, str],
        query_ids: list[str],
        affected_fields: list[str],
        observed_issue: str,
    ) -> str:
        if not any([mcp_server, api_info, query_ids, affected_fields, observed_issue]):
            return ""
        target = " ".join(part for part in [mcp_server, api_info.get("endpoint") or api_info.get("api_name")] if part) or "外部系统接口"
        query = f" 在查询 {', '.join(query_ids)} 时" if query_ids else ""
        fields = f"字段 {', '.join(affected_fields)}" if affected_fields else "完整业务字段"
        return f"修复 {target}{query}的数据返回逻辑，确保返回 {fields} 且与查询上下文一致。"

    def _external_task_title_from_context(self, context: dict[str, Any]) -> str:
        server = self._string(context.get("mcp_server"))
        api_name = self._string(context.get("api_name")) or self._string(context.get("endpoint")) or self._string(context.get("tool_name"))
        if server and api_name:
            return f"修复 {server} {api_name} 数据返回问题"
        if server:
            return f"修复 {server} 数据返回问题"
        return ""

    def _external_task_description_from_context(self, context: dict[str, Any]) -> str:
        server = self._string(context.get("mcp_server"))
        target = self._string(context.get("endpoint")) or self._string(context.get("api_name")) or self._string(context.get("tool_name"))
        observed_issue = self._string(context.get("observed_issue"))
        query_ids = self._string_list(context.get("query_ids"))
        if not (server and (target or observed_issue)):
            return ""
        query = f" 在查询 {', '.join(query_ids)} 时，" if query_ids else ""
        target_text = f" 的 {target}" if target else ""
        issue = f"具体表现为：{observed_issue}" if observed_issue else "存在数据不完整或与查询上下文不一致的问题。"
        return f"{server}{target_text}{query}返回的数据无法支撑 Agent 完成反馈场景回答。{issue}需要修复该接口或底层数据源，确保返回可被 Agent 稳定读取和使用的完整可靠数据。"

    def _external_task_objective_from_context(self, context: dict[str, Any]) -> str:
        server = self._string(context.get("mcp_server"))
        target = self._string(context.get("endpoint")) or self._string(context.get("api_name")) or self._string(context.get("tool_name"))
        if not server:
            return ""
        target_text = f" {target}" if target else ""
        return f"确保 {server}{target_text} 在同类查询中返回完整、可靠且与查询上下文匹配的数据，使 Agent 能基于返回结果完整回答反馈中指出的问题。"

    def _external_acceptance_criteria_from_context(self, context: dict[str, Any]) -> list[str]:
        server = self._string(context.get("mcp_server"))
        target = self._string(context.get("tool_name")) or self._string(context.get("endpoint")) or self._string(context.get("api_name"))
        query_ids = self._string_list(context.get("query_ids"))
        affected_fields = self._string_list(context.get("affected_fields"))
        observed_issue = self._string(context.get("observed_issue"))
        if not (server and (target or query_ids or affected_fields)):
            return []
        query = f" 在查询 {', '.join(query_ids)} 时" if query_ids else ""
        fields = f"，并包含 {', '.join(affected_fields)} 等关键字段" if affected_fields else ""
        criteria = [
            f"调用 {target or server}{query}，返回结果与查询上下文一致{fields}。",
        ]
        if observed_issue:
            criteria.append(f"返回结果不再出现该问题：{observed_issue}")
        criteria.append("关联回归测试中，Agent 能基于该返回结果完整回答反馈指出的问题。")
        return criteria

    def _plan_task_recommendation(self, target_type: str, execution_kind: str) -> str:
        if execution_kind == "workspace_execution":
            base = "由 execution-optimizer 根据归因结果生成受控执行方案，并在安全校验通过后应用到 workspace。"
        elif execution_kind == "external_webhook":
            base = "将该优化任务发送给对应外部系统，由外部系统处理服务、知识库、MCP server 或流程侧变更。"
        else:
            base = "当前归因结果不能转为 workspace 执行任务，也没有明确的外部 webhook 执行目标。"
        return base

    def _clean_plan_task_recommendation(self, value: Any, target_type: str, execution_kind: str) -> str:
        text = self._string(value) or self._plan_task_recommendation(target_type, execution_kind)
        marker = "归因依据："
        if marker in text:
            text = text.split(marker, 1)[0].rstrip()
        return text or self._plan_task_recommendation(target_type, execution_kind)

    def _evidence_summary(self, evidence_refs: list[dict[str, Any]]) -> str:
        summaries: list[str] = []
        for ref in evidence_refs[:5]:
            ref_id = self._string(ref.get("id")) or self._string(ref.get("path")) or self._string(ref.get("type")) or "evidence"
            reason = self._string(ref.get("reason"))
            summaries.append(f"{ref_id}: {reason}" if reason else ref_id)
        return "\n".join(summaries)

    def _external_owner_for_target(self, target_type: str) -> Optional[str]:
        if target_type == "external_mcp_service":
            return "external-mcp-service"
        if target_type == "soc_process":
            return "soc-process"
        if target_type == "mcp_description":
            return "mcp-owner"
        return None

    def _plan_target_path(self, target_type: str) -> Optional[str]:
        if target_type in {"main_agent_claude_md", "instruction_gap", "skill_gap", "tool_misuse", "evidence_gap"}:
            return "CLAUDE.md"
        if target_type == "mcp_config":
            return ".mcp.json"
        if target_type == "skill":
            return ".claude/skills/feedback-optimization.md"
        if target_type == "subagent":
            return ".claude/agents/feedback-optimization.md"
        if target_type == "output_style":
            return ".claude/output-styles/feedback-optimization.md"
        if target_type == "eval_case":
            return "evals/feedback-optimization.json"
        if target_type in {"mcp_description", "runtime_code", "external_mcp_service", "soc_process", "not_actionable"}:
            return None
        return "CLAUDE.md"

    def _external_webhook_by_alias(self, alias: str) -> dict[str, Any]:
        requested = self._string(alias)
        if not requested:
            raise ValueError("webhook_alias is required")
        if not self.external_webhooks_path.exists():
            raise ValueError(f"External governance webhook config not found: {self.external_webhooks_path}")
        try:
            loaded = yaml.safe_load(self.external_webhooks_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid external governance webhook config: {exc}") from exc
        for item in loaded.get("webhooks") or []:
            if not isinstance(item, dict):
                continue
            if self._string(item.get("alias")) == requested and self._string(item.get("url")):
                return {
                    "alias": requested,
                    "name": self._string(item.get("name")) or requested,
                    "url": self._string(item.get("url")),
                    "token": self._string(item.get("token")),
                    "timeout_seconds": int(item.get("timeout_seconds") or 5),
                }
        raise ValueError(f"Unknown external governance webhook alias: {requested}")

    def _external_notification_payload(self, item: dict[str, Any], webhook: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "schema_version": "external-governance-notification/v1",
            "webhook_alias": webhook["alias"],
            "external_item_id": item["external_item_id"],
            "feedback_case_id": item.get("feedback_case_id"),
            "proposal_job_id": item.get("proposal_job_id"),
            "title": item.get("title"),
            "description": item.get("description"),
            "objective": item.get("objective"),
            "target_summary": item.get("target_summary"),
            "owner": item.get("owner"),
            "actionability": item.get("actionability"),
            "recommendation": item.get("recommendation"),
            "recommended_actions": item.get("recommended_actions") or [],
            "acceptance_criteria": item.get("acceptance_criteria") or [],
            "expected_effect": item.get("expected_effect"),
            "validation": item.get("validation"),
            "risk": item.get("risk"),
            "analysis_summary": item.get("analysis_summary"),
            "evidence_summary": item.get("evidence_summary"),
            "evidence_refs": item.get("evidence_refs") or [],
            "reason": item.get("reason"),
            "created_at": item.get("created_at"),
        }
        for key in (
            "source",
            "batch_id",
            "optimization_plan_id",
            "plan_task_id",
            "target_type",
            "target_path",
            "task_context",
            "feedback_case_ids",
            "eval_case_ids",
            "source_attribution_job_ids",
        ):
            if item.get(key) is not None:
                payload[key] = item.get(key)
        return payload

    def _send_external_webhook(self, webhook: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if webhook.get("token"):
            headers["Authorization"] = f"Bearer {webhook['token']}"
        request = urlrequest.Request(
            str(webhook["url"]),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlrequest.urlopen(request, timeout=int(webhook.get("timeout_seconds") or 5)) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                return {"http_status": response.status, "response_body": body}
        except urlerror.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            return {"http_status": exc.code, "response_body": body}

    def _external_governance_item_to_dict(self, row: ExternalGovernanceItemModel) -> dict[str, Any]:
        item = dict(row.payload_json or {})
        item.update(
            {
                "external_item_id": row.external_item_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "feedback_case_id": row.feedback_case_id,
                "proposal_job_id": row.proposal_job_id,
                "owner": row.owner,
                "actionability": row.actionability,
                "latest_notification_id": row.latest_notification_id,
            }
        )
        with self.Session() as db:
            if row.latest_notification_id:
                notification = db.get(ExternalNotificationModel, row.latest_notification_id)
            else:
                notification = db.scalar(
                    select(ExternalNotificationModel)
                    .where(ExternalNotificationModel.external_item_id == row.external_item_id)
                    .order_by(ExternalNotificationModel.created_at.desc())
                    .limit(1)
                )
        if notification:
            item["latest_notification"] = dict(notification.payload_json or {})
        return item

    def _truncate(self, value: str, limit: int = 2000) -> str:
        return value if len(value) <= limit else f"{value[:limit]}..."

    def _case_model_from_dict(self, feedback_case: dict[str, Any]) -> FeedbackCaseModel:
        return FeedbackCaseModel(
            feedback_case_id=feedback_case["feedback_case_id"],
            created_at=feedback_case["created_at"],
            updated_at=feedback_case["updated_at"],
            status=feedback_case["status"],
            title=feedback_case["title"],
            priority=feedback_case["priority"],
            current_evidence_package_id=self._latest(feedback_case.get("evidence_package_ids")),
            current_attribution_job_id=self._latest(feedback_case.get("attribution_job_ids")),
            current_proposal_job_id=self._latest(feedback_case.get("proposal_job_ids")),
            source_ids_json=feedback_case.get("source_ids") or [],
            signal_ids_json=feedback_case.get("signal_ids") or [],
            event_ids_json=feedback_case.get("event_ids") or [],
            pending_correlation_ids_json=feedback_case.get("pending_correlation_ids") or [],
            run_ids_json=feedback_case.get("run_ids") or [],
            session_ids_json=feedback_case.get("session_ids") or [],
            alert_ids_json=feedback_case.get("alert_ids") or [],
            case_ids_json=feedback_case.get("case_ids") or [],
        )

    def _case_to_dict(self, row: FeedbackCaseModel) -> dict[str, Any]:
        return {
            "feedback_case_id": row.feedback_case_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "status": row.status,
            "title": row.title,
            "priority": row.priority,
            "source_ids": row.source_ids_json or [],
            "signal_ids": row.signal_ids_json or [],
            "event_ids": row.event_ids_json or [],
            "pending_correlation_ids": row.pending_correlation_ids_json or [],
            "run_ids": row.run_ids_json or [],
            "session_ids": row.session_ids_json or [],
            "alert_ids": row.alert_ids_json or [],
            "case_ids": row.case_ids_json or [],
            "evidence_package_ids": [row.current_evidence_package_id] if row.current_evidence_package_id else [],
            "attribution_job_ids": [row.current_attribution_job_id] if row.current_attribution_job_id else [],
            "proposal_job_ids": [row.current_proposal_job_id] if row.current_proposal_job_id else [],
        }

    def _job_record(
        self,
        *,
        job_id: str,
        job_type: str,
        feedback_case_id: str,
        evidence_package_id: str,
        status: str,
        profile_name: str,
        input_path: str,
        profile_version: Optional[dict[str, Any]] = None,
        attribution_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = utc_now()
        record = {
            "job_id": job_id,
            "job_type": job_type,
            "feedback_case_id": feedback_case_id,
            "evidence_package_id": evidence_package_id,
            "status": status,
            "profile_name": profile_name,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "timeout_seconds": 300,
            "retry_count": 0,
            "input_path": input_path,
            "raw_output_path": f"sqlite://feedback_jobs/{job_id}/raw_output_json",
            "validated_output_path": f"sqlite://feedback_jobs/{job_id}/validated_output_json",
            "error_path": f"sqlite://feedback_jobs/{job_id}/error_json",
            "langfuse_trace_id": None,
            "main_agent_version_id": self._current_agent_version_id(),
            "runtime_version": self.runtime_version,
            "schema_version": f"{job_type}-job/v1",
        }
        if profile_version:
            record["profile_version"] = profile_version
            record[f"{job_type}_agent_version"] = profile_version.get("agent_version")
        if attribution_job_id:
            record["attribution_job_id"] = attribution_job_id
        return record

    def _job_model_from_dict(self, job: dict[str, Any]) -> FeedbackJobModel:
        return FeedbackJobModel(
            job_id=job["job_id"],
            job_type=job["job_type"],
            feedback_case_id=job["feedback_case_id"],
            evidence_package_id=job["evidence_package_id"],
            attribution_job_id=self._string(job.get("attribution_job_id")),
            status=job["status"],
            profile_name=job["profile_name"],
            created_at=job["created_at"],
            started_at=self._string(job.get("started_at")),
            completed_at=self._string(job.get("completed_at")),
            input_path=job["input_path"],
            raw_output_path=job["raw_output_path"],
            validated_output_path=job["validated_output_path"],
            error_path=job["error_path"],
            langfuse_trace_id=self._string(job.get("langfuse_trace_id")),
            main_agent_version_id=self._string(job.get("main_agent_version_id")),
            runtime_version=job.get("runtime_version") or self.runtime_version,
            schema_version=job["schema_version"],
            timeout_seconds=int(job.get("timeout_seconds") or 300),
            retry_count=int(job.get("retry_count") or 0),
            profile_version_json=job.get("profile_version"),
            input_json=job.get("input_json"),
        )

    def _job_to_dict(self, row: FeedbackJobModel) -> dict[str, Any]:
        job = {
            "job_id": row.job_id,
            "job_type": row.job_type,
            "feedback_case_id": row.feedback_case_id,
            "evidence_package_id": row.evidence_package_id,
            "status": row.status,
            "profile_name": row.profile_name,
            "created_at": row.created_at,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "timeout_seconds": row.timeout_seconds,
            "retry_count": row.retry_count,
            "input_path": row.input_path,
            "raw_output_path": row.raw_output_path,
            "validated_output_path": row.validated_output_path,
            "error_path": row.error_path,
            "langfuse_trace_id": row.langfuse_trace_id,
            "main_agent_version_id": row.main_agent_version_id,
            "runtime_version": row.runtime_version,
            "schema_version": row.schema_version,
            "input_json": row.input_json,
            "raw_output_json": row.raw_output_json,
            "validated_output_json": row.validated_output_json,
            "error_json": self._normalize_job_error_payload(row.error_json),
        }
        if row.profile_version_json:
            job["profile_version"] = row.profile_version_json
            job[f"{row.job_type}_agent_version"] = row.profile_version_json.get("agent_version")
        if row.attribution_job_id:
            job["attribution_job_id"] = row.attribution_job_id
        return job

    def _job_batch_id(self, job: dict[str, Any]) -> Optional[str]:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        return self._string(input_json.get("batch_id"))

    def _append_job_update(
        self,
        job_id: str,
        *,
        status: str,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with self.Session.begin() as db:
            job = db.get(FeedbackJobModel, job_id)
            if not job:
                return None
            job.status = status
            if started_at is not None:
                job.started_at = started_at
            if completed_at is not None:
                job.completed_at = completed_at
        return self.get_job(job_id)

    def _set_job_json(
        self,
        job_id: str,
        *,
        raw_output_json: Optional[dict[str, Any]] = None,
        validated_output_json: Optional[dict[str, Any]] = None,
        error_json: Optional[dict[str, Any]] = None,
    ) -> None:
        with self.Session.begin() as db:
            job = db.get(FeedbackJobModel, job_id)
            if not job:
                return
            if raw_output_json is not None:
                job.raw_output_json = raw_output_json
            if validated_output_json is not None:
                job.validated_output_json = validated_output_json
            if error_json is not None:
                job.error_json = error_json

    def _append_case_update(
        self,
        feedback_case: dict[str, Any],
        *,
        status: Optional[str] = None,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.Session.begin() as db:
            row = db.get(FeedbackCaseModel, feedback_case["feedback_case_id"])
            if not row:
                return feedback_case
            row.updated_at = utc_now()
            row.status = status or row.status
            if evidence_package_id:
                row.current_evidence_package_id = evidence_package_id
            if attribution_job_id:
                row.current_attribution_job_id = attribution_job_id
            if proposal_job_id:
                row.current_proposal_job_id = proposal_job_id
        return self.find_case(feedback_case["feedback_case_id"]) or feedback_case

    def _write_job_error(self, job: dict[str, Any], error_code: str, message: str) -> None:
        error_payload: dict[str, Any] = {"error_code": error_code, "message": message, "created_at": utc_now(), "job_id": job["job_id"]}
        error_payload = self._normalize_job_error_payload(error_payload)
        self._set_job_json(
            job["job_id"],
            error_json=error_payload,
        )

    def _normalize_job_error_payload(self, error_payload: Any) -> Any:
        if not isinstance(error_payload, dict):
            return error_payload
        message = error_payload.get("message")
        if not isinstance(message, str):
            return error_payload
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return error_payload
        if isinstance(parsed, list):
            return {**error_payload, "message": "分析 Agent 输出不符合 schema。", "validation_errors": parsed}
        return error_payload

    def _latest_reusable_job(self, feedback_case_id: str, job_type: str) -> Optional[dict[str, Any]]:
        if job_type == "attribution":
            feedback_case = self.find_case(feedback_case_id)
            current_job_id = self._latest((feedback_case or {}).get("attribution_job_ids"))
            if not current_job_id:
                return None
            job = self.get_job(current_job_id)
            if not job:
                return None
            if job.get("status") == "failed":
                self.discard_current_attribution(feedback_case_id, invalidate_downstream=True)
                return None
            if self._job_is_stale(job):
                self.discard_current_attribution(feedback_case_id, invalidate_downstream=True)
                return None
            return job
        with self.Session() as db:
            row = db.scalar(
                select(FeedbackJobModel)
                .where(FeedbackJobModel.feedback_case_id == feedback_case_id, FeedbackJobModel.job_type == job_type)
                .order_by(FeedbackJobModel.created_at.desc())
                .limit(1)
            )
            if not row or row.status == "failed":
                return None
            return self._job_to_dict(row)

    def _job_is_stale(self, job: dict[str, Any]) -> bool:
        if job.get("status") not in {"created", "queued", "running", "schema_validating", "evidence_packaging"}:
            return False
        base = self._parse_datetime(self._string(job.get("started_at")) or self._string(job.get("created_at")))
        if not base:
            return False
        timeout_seconds = int(job.get("timeout_seconds") or 300)
        return datetime.now(timezone.utc) >= base + timedelta(seconds=timeout_seconds)

    def _proposal_model_from_dict(self, proposal: dict[str, Any]) -> OptimizationProposalModel:
        return OptimizationProposalModel(
            proposal_id=proposal["proposal_id"],
            feedback_case_id=proposal["feedback_case_id"],
            proposal_job_id=proposal["proposal_job_id"],
            status=proposal["status"],
            actionability=self._string(proposal.get("actionability")),
            target_path=self._string(proposal.get("target_path")),
            created_at=proposal["created_at"],
            payload_json=proposal,
        )

    def _proposal_to_dict(self, row: OptimizationProposalModel) -> dict[str, Any]:
        proposal = dict(row.payload_json or {})
        proposal["status"] = row.status
        with self.Session() as db:
            review = db.scalar(
                select(ProposalReviewModel)
                .where(ProposalReviewModel.proposal_id == row.proposal_id)
                .order_by(ProposalReviewModel.created_at.desc())
                .limit(1)
            )
        if review:
            proposal["latest_review"] = review.payload_json
        return proposal

    def _supersede_case_proposals(
        self,
        feedback_case_id: str,
        *,
        reason: str,
        superseded_by_job_id: str,
    ) -> dict[str, int]:
        superseded_at = utc_now()
        proposal_count = 0
        external_count = 0
        with self.Session.begin() as db:
            proposals = db.scalars(
                select(OptimizationProposalModel).where(
                    OptimizationProposalModel.feedback_case_id == feedback_case_id,
                    OptimizationProposalModel.status.in_(("pending_review", "needs_more_analysis")),
                )
            ).all()
            for row in proposals:
                payload = dict(row.payload_json or {})
                row.status = "superseded"
                row.payload_json = {
                    **payload,
                    "status": "superseded",
                    "superseded_at": superseded_at,
                    "superseded_reason": reason,
                    "superseded_by_job_id": superseded_by_job_id,
                }
                proposal_count += 1

            external_items = db.scalars(
                select(ExternalGovernanceItemModel).where(
                    ExternalGovernanceItemModel.feedback_case_id == feedback_case_id,
                    ExternalGovernanceItemModel.status.in_(("pending_notification", "notification_failed")),
                )
            ).all()
            for row in external_items:
                payload = dict(row.payload_json or {})
                row.status = "superseded"
                row.updated_at = superseded_at
                row.payload_json = {
                    **payload,
                    "status": "superseded",
                    "updated_at": superseded_at,
                    "superseded_at": superseded_at,
                    "superseded_reason": reason,
                    "superseded_by_job_id": superseded_by_job_id,
                }
                external_count += 1
        return {"proposals": proposal_count, "external_guidance_items": external_count}

    def _update_task_payload(
        self,
        task_id: str,
        *,
        status: str,
        fields: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        with self.Session.begin() as db:
            row = db.get(OptimizationTaskModel, task_id)
            if not row:
                return None
            payload = dict(row.payload_json or {})
            payload.update(fields)
            payload["status"] = status
            row.status = status
            row.payload_json = payload
        return self.find_task(task_id)

    def _latest_execution_job(self, task_id: str) -> Optional[dict[str, Any]]:
        with self.Session() as db:
            row = db.scalars(
                select(OptimizationExecutionModel)
                .where(OptimizationExecutionModel.optimization_task_id == task_id)
                .order_by(OptimizationExecutionModel.created_at.desc())
            ).first()
            return self._execution_job_to_dict(row) if row else None

    def _execution_job_to_dict(self, row: OptimizationExecutionModel) -> dict[str, Any]:
        payload = dict(row.payload_json or {})
        payload["execution_job_id"] = row.execution_job_id
        payload["optimization_task_id"] = row.optimization_task_id
        payload["feedback_case_id"] = row.feedback_case_id
        payload["proposal_id"] = row.proposal_id
        payload["status"] = row.status
        payload["profile_name"] = row.profile_name
        payload["created_at"] = row.created_at
        payload["started_at"] = row.started_at
        payload["completed_at"] = row.completed_at
        payload["baseline_agent_version_id"] = row.baseline_agent_version_id
        return payload

    def _update_execution_job_payload(
        self,
        execution_job_id: str,
        *,
        status: str,
        fields: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        with self.Session.begin() as db:
            row = db.get(OptimizationExecutionModel, execution_job_id)
            if not row:
                return None
            payload = dict(row.payload_json or {})
            payload.update(fields)
            payload["status"] = status
            row.status = status
            if fields.get("started_at") is not None:
                row.started_at = self._string(fields.get("started_at"))
            if fields.get("completed_at") is not None:
                row.completed_at = self._string(fields.get("completed_at"))
            row.payload_json = payload
        return self.get_execution_job(execution_job_id)

    def _attach_execution_job_to_task(self, task_id: str, job: dict[str, Any], *, status: str) -> Optional[dict[str, Any]]:
        task = self.find_task(task_id)
        if not task:
            return None
        job_id = self._string(job.get("execution_job_id"))
        job_ids = [str(item) for item in task.get("execution_job_ids") or [] if item]
        if job_id and job_id not in job_ids:
            job_ids.append(job_id)
        fields = {
            "execution_job_ids": job_ids,
            "latest_execution_job_id": job_id,
            "latest_execution_job": job,
        }
        if job.get("baseline_agent_version_id"):
            fields["baseline_agent_version_id"] = job.get("baseline_agent_version_id")
        if job.get("pre_execution_agent_version_id"):
            fields["pre_execution_agent_version_id"] = job.get("pre_execution_agent_version_id")
            fields["pre_execution_agent_version"] = job.get("pre_execution_agent_version")
        if job.get("applied_agent_version_id"):
            fields["applied_agent_version_id"] = job.get("applied_agent_version_id")
            fields["applied_agent_version"] = job.get("applied_agent_version")
            fields["applied_at"] = job.get("completed_at") or utc_now()
            fields["application_note"] = f"execution-optimizer 应用执行方案 {job.get('execution_job_id')}。"
        return self._update_task_payload(task_id, status=status, fields=fields)

    def _sanitize_execution_plan(self, plan: dict[str, Any], job: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        sanitized = dict(plan)
        sanitized["execution_job_id"] = job["execution_job_id"]
        sanitized["optimization_task_id"] = job["optimization_task_id"]
        sanitized["baseline_agent_version_id"] = sanitized.get("baseline_agent_version_id") or job.get("baseline_agent_version_id")
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        target_paths = set(str(path) for path in input_json.get("target_paths") or [])
        target_contexts = {
            str(item.get("path")): item
            for item in input_json.get("target_file_contexts") or []
            if isinstance(item, dict) and item.get("path")
        }
        operations = []
        seen_write_paths: set[str] = set()
        for item in sanitized.get("operations") or []:
            if not isinstance(item, dict):
                return None, "operations must be objects"
            operation = dict(item)
            path = self._string(operation.get("path"))
            if not path or path not in target_paths:
                return None, f"operation path is not in task target_paths: {path or '-'}"
            if not self._target_allowed(path):
                return None, f"operation path is not allowed: {path}"
            op = self._string(operation.get("operation"))
            if op not in {"append_text", "replace_file", "create_file", "noop"}:
                return None, f"operation is not supported: {op or '-'}"
            context = target_contexts.get(path) or self._execution_target_file_context(path)
            skipped_reason = self._string(context.get("skipped_reason")) if isinstance(context, dict) else None
            if op != "noop":
                if skipped_reason:
                    return None, f"operation target is not safely editable: {path} ({skipped_reason})"
                if path in seen_write_paths:
                    return None, f"multiple write operations for one path are not supported: {path}"
                seen_write_paths.add(path)
            if op == "append_text" and not isinstance(operation.get("append_text"), str):
                return None, f"append_text operation must include append_text: {path}"
            if op in {"replace_file", "create_file"} and not isinstance(operation.get("content"), str):
                return None, f"{op} operation must include content: {path}"
            if op in {"append_text", "replace_file"}:
                if not context.get("exists") or context.get("type") != "file":
                    return None, f"{op} target must be an existing managed text file: {path}"
                expected_sha = self._string(context.get("sha256"))
                if expected_sha and not operation.get("expected_sha256"):
                    operation["expected_sha256"] = expected_sha
            if op == "create_file" and context.get("exists"):
                return None, f"create_file target already exists: {path}"
            operations.append(operation)
        sanitized["operations"] = operations
        if sanitized.get("status") == "ready" and not operations:
            return None, "ready execution plan has no operations"
        return sanitized, None

    def _attach_task_regression_run(self, task_id: str, eval_run: dict[str, Any], *, status: str) -> Optional[dict[str, Any]]:
        task = self.find_task(task_id)
        if not task:
            return None
        run_id = self._string(eval_run.get("eval_run_id"))
        run_ids = [str(item) for item in task.get("regression_run_ids") or [] if item]
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
        return self._update_task_payload(
            task_id,
            status=status,
            fields={
                "regression_run_ids": run_ids,
                "latest_regression_run_id": run_id,
                "latest_regression_run": eval_run,
                "regression_completed_at": eval_run.get("completed_at"),
            },
        )

    def _build_eval_case_from_feedback(self, feedback_case: dict[str, Any]) -> Optional[dict[str, Any]]:
        attribution_job_id = self._latest(feedback_case.get("attribution_job_ids"))
        proposal_job_id = self._latest(feedback_case.get("proposal_job_ids"))
        if not attribution_job_id or not proposal_job_id:
            return None

        attribution_output = self.get_job_output(attribution_job_id, "attribution") or {}
        proposal_output = self.get_job_output(proposal_job_id, "proposal") or {}
        if not attribution_output or not proposal_output:
            return None

        source_run_id = self._latest(feedback_case.get("run_ids"))
        source_run = self.find_run(run_id=source_run_id) if source_run_id else None
        prompt = self._string((source_run or {}).get("message")) or self._string(feedback_case.get("title"))
        if not prompt:
            return None

        signals = [signal for signal in (self.find_signal(signal_id) for signal_id in feedback_case.get("signal_ids", [])) if signal]
        labels = self._unique_strings(
            [
                *[str(label) for signal in signals for label in (signal.get("labels") or [])],
                self._string(attribution_output.get("problem_type")) or "",
                self._string(attribution_output.get("optimization_object_type")) or "",
            ]
        )
        proposals = [item for item in proposal_output.get("proposals") or [] if isinstance(item, dict)]
        primary_proposal = proposals[0] if proposals else {}
        expected_behavior = self._eval_expected_behavior(feedback_case, attribution_output, primary_proposal)
        checks_json = self._eval_checks(labels, attribution_output, primary_proposal)
        created_at = utc_now()
        return {
            "schema_version": "feedback-eval-case/v1",
            "eval_case_id": f"evc-{uuid.uuid4()}",
            "created_at": created_at,
            "updated_at": created_at,
            "status": "active",
            "source": "feedback_dataset",
            "source_feedback_case_id": feedback_case["feedback_case_id"],
            "source_run_id": source_run_id,
            "source_signal_ids": feedback_case.get("signal_ids") or [],
            "source_evidence_package_id": self._latest(feedback_case.get("evidence_package_ids")),
            "source_attribution_job_id": attribution_job_id,
            "source_proposal_job_id": proposal_job_id,
            "prompt": prompt,
            "labels": labels,
            "expected_behavior": expected_behavior,
            "checks_json": checks_json,
            "source_summary": {
                "feedback_title": feedback_case.get("title"),
                "feedback_status": feedback_case.get("status"),
                "feedback_comments": [signal.get("comment") for signal in signals if signal.get("comment")],
                "original_answer_summary": (source_run or {}).get("answer_summary"),
            },
            "attribution_summary": {
                "problem_type": attribution_output.get("problem_type"),
                "optimization_object_type": attribution_output.get("optimization_object_type"),
                "actionability": attribution_output.get("actionability"),
                "confidence": attribution_output.get("confidence"),
                "rationale": attribution_output.get("rationale"),
            },
            "proposal_summary": {
                "proposal_id": primary_proposal.get("proposal_id"),
                "title": primary_proposal.get("title"),
                "target_type": primary_proposal.get("target_type"),
                "target_path": primary_proposal.get("target_path"),
                "validation": primary_proposal.get("validation"),
                "expected_effect": primary_proposal.get("expected_effect"),
            },
        }

    def _eval_expected_behavior(
        self,
        feedback_case: dict[str, Any],
        attribution_output: dict[str, Any],
        proposal: dict[str, Any],
    ) -> str:
        validation = self._string(proposal.get("validation"))
        recommendation = self._string(proposal.get("recommendation"))
        problem_type = self._string(attribution_output.get("problem_type")) or "反馈问题"
        title = self._string(feedback_case.get("title")) or "原反馈场景"
        parts = [
            f"复测“{title}”对应的原始输入，回答应纠正 {problem_type}。",
            validation or recommendation or "输出应完整、可核查，并符合当前主智能体配置。",
        ]
        return " ".join(part for part in parts if part)

    def _eval_checks(
        self,
        labels: list[str],
        attribution_output: dict[str, Any],
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        label_set = set(labels)
        problem_type = self._string(attribution_output.get("problem_type"))
        target_type = self._string(proposal.get("target_type")) or self._string(attribution_output.get("optimization_object_type"))
        requires_tool_use = bool(
            label_set
            & {
                "tool_data_incomplete",
                "tool_data_quality",
                "tool_misuse",
                "tool_unavailable",
                "evidence_gap",
            }
        ) or problem_type in {"tool_data_quality", "tool_misuse", "tool_unavailable", "evidence_gap"}
        preferred_tools = ["Read", "Grep", "Glob"] if target_type in {"main_agent_claude_md", "skill", "subagent", "mcp_config"} else []
        return {
            "requires_non_empty_answer": True,
            "requires_no_runtime_errors": True,
            "requires_tool_use": requires_tool_use,
            "preferred_tools": preferred_tools,
            "notes": "首版使用确定性运行信号评估；语义质量保留人工复核入口。",
        }

    def _eval_case_to_dict(self, row: EvalCaseModel) -> dict[str, Any]:
        payload = dict(row.payload_json or {})
        payload["eval_case_id"] = row.eval_case_id
        payload["created_at"] = row.created_at
        payload["updated_at"] = row.updated_at
        payload["status"] = row.status
        payload["source_feedback_case_id"] = row.source_feedback_case_id
        payload["source_run_id"] = row.source_run_id
        payload["labels"] = list(row.labels_json or payload.get("labels") or [])
        return payload

    def _eval_run_to_dict(self, row: EvalRunModel) -> dict[str, Any]:
        payload = dict(row.payload_json or {})
        payload["eval_run_id"] = row.eval_run_id
        payload["created_at"] = row.created_at
        payload["completed_at"] = row.completed_at
        payload["status"] = row.status
        payload["agent_version_id"] = row.agent_version_id
        payload["optimization_task_id"] = row.optimization_task_id
        payload["source"] = row.source
        with self.Session() as db:
            items = [
                item.payload_json
                for item in db.scalars(
                    select(EvalRunItemModel)
                    .where(EvalRunItemModel.eval_run_id == row.eval_run_id)
                    .order_by(EvalRunItemModel.eval_run_item_id.asc())
                ).all()
            ]
        payload["items"] = items
        return payload

    def _current_agent_version_id(self) -> Optional[str]:
        if not self.agent_version_provider:
            return None
        try:
            return self.agent_version_provider()
        except Exception:
            return None

    def _case_title(self, records: list[dict[str, Any]]) -> str:
        for record in records:
            comment = self._string(record.get("comment"))
            if comment:
                return comment[:120]
            event_type = self._string(record.get("event_type"))
            if event_type:
                return event_type
            labels = record.get("labels")
            if isinstance(labels, list) and labels:
                return ", ".join(map(str, labels[:3]))
        return "反馈处置单"

    def _same_case_or_alert(self, run: dict[str, Any], alert_id: Optional[str], case_id: Optional[str]) -> bool:
        return bool((alert_id and run.get("alert_id") == alert_id) or (case_id and run.get("case_id") == case_id))

    def _execution_target_policy(self) -> dict[str, Any]:
        return {
            "type": "main_workspace_managed_full_with_excludes",
            "snapshot_policy_version": SNAPSHOT_POLICY_VERSION,
            "workspace_root": str(self.main_workspace_dir),
            "excluded_names": sorted(WORKSPACE_EXCLUDED_NAMES),
            "excluded_patterns": list(WORKSPACE_EXCLUDED_PATTERNS),
            "max_inline_text_bytes": MAX_EXECUTION_TARGET_CONTEXT_BYTES,
        }

    def _execution_target_file_contexts(self, target_paths: list[str]) -> list[dict[str, Any]]:
        return [self._execution_target_file_context(path) for path in target_paths]

    def _execution_target_file_context(self, target_path: str) -> dict[str, Any]:
        context: dict[str, Any] = {
            "path": target_path,
            "managed": False,
            "exists": False,
            "type": "missing",
            "size_bytes": None,
            "sha256": None,
            "content_encoding": None,
            "content_text": None,
            "skipped_reason": None,
        }
        denied = self._target_denied_reason(target_path)
        if denied:
            context["skipped_reason"] = denied
            return context
        context["managed"] = True
        dest = self._workspace_target_path(target_path)
        if not dest:
            context["skipped_reason"] = "target_path_escapes_workspace"
            return context
        try:
            stat = dest.lstat()
        except FileNotFoundError:
            return context
        except OSError as exc:
            context["skipped_reason"] = f"stat_failed:{exc.__class__.__name__}"
            return context
        context["exists"] = True
        if dest.is_symlink():
            context["type"] = "symlink"
            context["size_bytes"] = len(str(dest.readlink()))
            context["skipped_reason"] = "symlink_target_not_auto_editable"
            return context
        if dest.is_dir():
            context["type"] = "dir"
            context["size_bytes"] = 0
            context["skipped_reason"] = "directory_target_not_auto_editable"
            return context
        if not dest.is_file():
            context["type"] = "other"
            context["size_bytes"] = stat.st_size
            context["skipped_reason"] = "special_file_not_auto_editable"
            return context
        context["type"] = "file"
        context["size_bytes"] = stat.st_size
        try:
            data = dest.read_bytes()
        except OSError as exc:
            context["skipped_reason"] = f"read_failed:{exc.__class__.__name__}"
            return context
        context["sha256"] = hashlib.sha256(data).hexdigest()
        if len(data) > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
            context["skipped_reason"] = "file_too_large_for_inline_context"
            return context
        if b"\x00" in data:
            context["skipped_reason"] = "binary_file_not_auto_editable"
            return context
        try:
            context["content_text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            context["skipped_reason"] = "non_utf8_file_not_auto_editable"
            return context
        context["content_encoding"] = "utf-8"
        return context

    def _target_allowed(self, target_path: str) -> bool:
        return self._target_denied_reason(target_path) is None

    def _target_denied_reason(self, target_path: str) -> Optional[str]:
        rel = self._workspace_relative_path(target_path)
        if rel is None:
            return "unsafe_target_path"
        if self._workspace_rel_excluded(rel):
            return "workspace_excluded_path"
        if not self._workspace_target_path(target_path):
            return "target_path_escapes_workspace"
        return None

    def _workspace_relative_path(self, target_path: str) -> Optional[Path]:
        if not isinstance(target_path, str):
            return None
        raw = target_path.strip()
        if not raw or "\\" in raw:
            return None
        rel = Path(raw)
        if rel.is_absolute() or rel == Path(".") or ".." in rel.parts:
            return None
        return rel

    def _workspace_rel_excluded(self, rel: Path) -> bool:
        parts = rel.parts
        if any(part in WORKSPACE_EXCLUDED_NAMES for part in parts):
            return True
        name = rel.name
        return any(fnmatch.fnmatch(name, pattern) for pattern in WORKSPACE_EXCLUDED_PATTERNS)

    def _workspace_target_path(self, target_path: str) -> Optional[Path]:
        rel = self._workspace_relative_path(target_path)
        if rel is None:
            return None
        base = self.main_workspace_dir.resolve()
        dest = (base / rel).resolve(strict=False)
        if base != dest and base not in dest.parents:
            return None
        return dest

    def _evidence_payload(self, value: Any) -> Any:
        if self.enable_debug_evidence:
            return value
        return self._scrub_record(value)

    def _langfuse_trace_refs(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for run in runs:
            trace_id = self._string(run.get("langfuse_trace_id"))
            trace_url = self._string(run.get("langfuse_trace_url"))
            if not trace_id and not trace_url:
                continue
            refs.append({"run_id": run.get("run_id"), "session_id": run.get("session_id"), "trace_id": trace_id, "trace_url": trace_url})
        return refs

    def _scrub_record(self, value: Any) -> Any:
        if isinstance(value, dict):
            clean: dict[str, Any] = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                    clean[key] = "[REDACTED]"
                else:
                    clean[key] = self._scrub_record(item)
            return clean
        if isinstance(value, list):
            return [self._scrub_record(item) for item in value]
        return value

    def _filter_records(
        self,
        records: list[dict[str, Any]],
        filters: dict[str, Any],
        limit: int,
        *,
        any_key_groups: Optional[list[tuple[str, ...]]] = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        any_key_groups = any_key_groups or []
        for record in records:
            if self._matches_filters(record, filters, any_key_groups):
                result.append(record)
            if len(result) >= limit:
                break
        return result

    def _matches_filters(self, record: dict[str, Any], filters: dict[str, Any], any_key_groups: list[tuple[str, ...]]) -> bool:
        grouped_keys = {key for group in any_key_groups for key in group}
        for key, value in filters.items():
            if value in (None, "") or key in grouped_keys:
                continue
            if record.get(key) != value:
                return False
        for group in any_key_groups:
            expected = next((filters.get(key) for key in group if filters.get(key) not in (None, "")), None)
            if expected is None:
                continue
            if not any(record.get(key) == expected for key in group):
                return False
        return True

    def _materialize_evidence_files(self, job_id: str, job_type: str, evidence_package_id: str, names: Iterable[str]) -> list[str]:
        paths: list[str] = []
        evidence_dir = self.tmp_jobs_dir / job_id / job_type / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            evidence_file = self.get_evidence_package_file(evidence_package_id, name)
            if not evidence_file:
                continue
            path = evidence_dir / name
            self._write_json(path, evidence_file["content"])
            paths.append(str(path))
        return paths

    def _materialize_manifest(self, job_id: str, job_type: str, evidence_package_id: str) -> str:
        manifest = self.get_evidence_package(evidence_package_id) or {}
        path = self.tmp_jobs_dir / job_id / job_type / "evidence" / "manifest.json"
        self._write_json(path, manifest)
        return str(path)

    def _materialize_extra_json(self, job_id: str, job_type: str, file_name: str, payload: dict[str, Any]) -> str:
        path = self.tmp_jobs_dir / job_id / job_type / file_name
        self._write_json(path, payload)
        return str(path)

    def _write_job_input(self, job_id: str, job_type: str, payload: dict[str, Any]) -> str:
        path = self.tmp_jobs_dir / job_id / job_type / "input.json"
        self._write_json(path, payload)
        return str(path)

    def _cleanup_job_tmp(self, job_id: str) -> None:
        shutil.rmtree(self.tmp_jobs_dir / job_id, ignore_errors=True)

    def _discard_job(self, job_id: str) -> None:
        if not job_id:
            return
        with self.Session.begin() as db:
            row = db.get(FeedbackJobModel, job_id)
            if row:
                db.delete(row)
        self._cleanup_job_tmp(job_id)

    def _discard_proposal_job(self, proposal_job_id: str) -> None:
        if not proposal_job_id:
            return
        with self.Session.begin() as db:
            proposals = db.scalars(select(OptimizationProposalModel).where(OptimizationProposalModel.proposal_job_id == proposal_job_id)).all()
            proposal_ids = [proposal.proposal_id for proposal in proposals]
            for proposal_id in proposal_ids:
                db.execute(delete(ProposalReviewModel).where(ProposalReviewModel.proposal_id == proposal_id))
            if proposal_ids:
                db.execute(delete(OptimizationProposalModel).where(OptimizationProposalModel.proposal_id.in_(proposal_ids)))
            external_items = db.scalars(select(ExternalGovernanceItemModel).where(ExternalGovernanceItemModel.proposal_job_id == proposal_job_id)).all()
            for item in external_items:
                notifications = db.scalars(select(ExternalNotificationModel).where(ExternalNotificationModel.external_item_id == item.external_item_id)).all()
                for notification in notifications:
                    db.delete(notification)
                db.delete(item)
            row = db.get(FeedbackJobModel, proposal_job_id)
            if row:
                db.delete(row)
        self._cleanup_job_tmp(proposal_job_id)

    def _discard_batch_draft_artifacts(self, batch: dict[str, Any]) -> None:
        task_id = self._string(batch.get("optimization_task_id"))
        execution_job_id = self._string(batch.get("execution_job_id"))
        internal_proposal_id = self._string(batch.get("internal_proposal_id"))
        with self.Session.begin() as db:
            if task_id:
                execution_rows = db.scalars(select(OptimizationExecutionModel).where(OptimizationExecutionModel.optimization_task_id == task_id)).all()
                for execution in execution_rows:
                    db.delete(execution)
                    self._cleanup_job_tmp(execution.execution_job_id)
                task = db.get(OptimizationTaskModel, task_id)
                if task and not (task.payload_json or {}).get("applied_agent_version_id"):
                    db.delete(task)
            if execution_job_id:
                execution = db.get(OptimizationExecutionModel, execution_job_id)
                if execution:
                    db.delete(execution)
                self._cleanup_job_tmp(execution_job_id)
            if internal_proposal_id:
                db.execute(delete(ProposalReviewModel).where(ProposalReviewModel.proposal_id == internal_proposal_id))
                db.execute(delete(OptimizationProposalModel).where(OptimizationProposalModel.proposal_id == internal_proposal_id))

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        return None

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        return []

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _sha256_json(self, value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _unique_strings(self, values: Iterable[Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _string_list(self, values: Any) -> list[str]:
        if isinstance(values, str):
            return [values] if values else []
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, str) and item]

    def _short_text(self, value: Optional[str], limit: int = 420) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return ""
        return text if len(text) <= limit else f"{text[:limit]}..."

    def _latest(self, values: Any) -> Optional[str]:
        if not isinstance(values, list) or not values:
            return None
        value = values[-1]
        return value if isinstance(value, str) and value else None

    def _string(self, value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
