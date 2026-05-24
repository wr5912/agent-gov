from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import or_, select

from .feedback_schemas import validate_attribution_output, validate_proposal_output
from .runtime_db import (
    AgentRunModel,
    EvidenceFileModel,
    EvidencePackageModel,
    FeedbackCaseModel,
    FeedbackJobModel,
    FeedbackSignalModel,
    OptimizationProposalModel,
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

DIRECT_TARGET_PREFIXES = (
    "CLAUDE.md",
    ".mcp.json",
    ".claude/settings.json",
    ".claude/skills/",
    ".claude/agents/",
    ".claude/output-styles/",
    "evals/",
)


class FeedbackStore:
    """SQLAlchemy-backed store for the feedback optimization loop."""

    def __init__(
        self,
        *,
        data_dir: Path,
        agent_version_provider: Optional[Callable[[], Optional[str]]] = None,
        runtime_version: str = "0.2.0",
        enable_debug_evidence: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
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
    ) -> Optional[dict[str, Any]]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        existing = self._latest_reusable_job(feedback_case_id, "attribution")
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
            profile_name="feedback-attribution",
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
    ) -> Optional[dict[str, Any]]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        existing = self._latest_reusable_job(feedback_case_id, "proposal")
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
            "allowed_target_paths": list(DIRECT_TARGET_PREFIXES),
            "task": "generate_optimization_proposals",
        }
        input_path = self._write_job_input(job_id, "proposal", input_payload)
        job = self._job_record(
            job_id=job_id,
            job_type="proposal",
            feedback_case_id=feedback_case_id,
            evidence_package_id=evidence_package_id,
            status="queued",
            profile_name="feedback-proposal",
            input_path=input_path,
            profile_version=profile_version,
            attribution_job_id=attribution_job_id,
        )
        job["input_json"] = input_payload
        with self.Session.begin() as db:
            db.add(self._job_model_from_dict(job))
        self._append_case_update(feedback_case, proposal_job_id=job_id, status="proposal_queued")
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
                self._append_case_update(feedback_case, status="pending_attribution")
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
                self._append_case_update(feedback_case, status="pending_proposal")
            completed = self._append_job_update(job_id, status="needs_human_review", completed_at=utc_now())
            self._cleanup_job_tmp(job_id)
            return completed

        normalized = self._normalize_proposal_output(validated, job)
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

    def list_tasks(self, *, feedback_case_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as db:
            tasks = [row.payload_json for row in db.scalars(select(OptimizationTaskModel).order_by(OptimizationTaskModel.created_at.desc())).all()]
        return self._filter_records(tasks, {"feedback_case_id": feedback_case_id, "status": status}, limit)

    def find_task(self, task_id: str) -> Optional[dict[str, Any]]:
        if not task_id:
            return None
        with self.Session() as db:
            row = db.get(OptimizationTaskModel, task_id)
            return row.payload_json if row else None

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
                    "reason": "证据包已固化；当前未配置模型提供商，需人工或归因 Agent 补充分析。",
                }
            ],
            "responsibility_boundary": {"owner": "needs_human_analysis", "reason": "未形成可安全转为主 Agent workspace 修改的归因结论。"},
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
                    "recommendation": "当前没有高置信归因输出，不能创建主 Agent workspace 修改建议。",
                    "reason": "归因 job 未给出 direct_workspace_change 或 workspace_config_change。",
                }
            ],
            "no_action_reason": "needs_human_analysis",
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
        with self.Session() as db:
            row = db.scalar(
                select(FeedbackJobModel)
                .where(FeedbackJobModel.feedback_case_id == feedback_case_id, FeedbackJobModel.job_type == job_type)
                .order_by(FeedbackJobModel.created_at.desc())
                .limit(1)
            )
            if not row or row.status in {"failed", "needs_human_review"}:
                return None
            return self._job_to_dict(row)

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

    def _target_allowed(self, target_path: str) -> bool:
        if target_path.startswith("/") or ".." in Path(target_path).parts:
            return False
        return any(target_path == prefix or target_path.startswith(prefix) for prefix in DIRECT_TARGET_PREFIXES)

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

    def _latest(self, values: Any) -> Optional[str]:
        if not isinstance(values, list) or not values:
            return None
        value = values[-1]
        return value if isinstance(value, str) and value else None

    def _string(self, value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None
