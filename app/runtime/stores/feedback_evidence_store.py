from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from ..feedback_privacy import SENSITIVE_KEY_PARTS
from ..records.evidence_records import EvidenceIncludedFileRecord, EvidencePackageFileRecord, EvidencePackageRecord
from ..records.json_types import JsonObject
from ..runtime_db import EvidenceFileModel, EvidencePackageModel, utc_now


class FeedbackEvidenceStoreMixin:
    """Store operations for evidence package manifests, files, and job materialization."""

    def create_evidence_package(self, feedback_case_id: str) -> Optional[JsonObject]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        existing_id = self._latest(feedback_case.get("evidence_package_ids"))
        if existing_id:
            existing = self.get_evidence_package(existing_id)
            if existing:
                return existing

        evidence_id = f"evp-{uuid.uuid4()}"
        context = self._collect_evidence_context(feedback_case)
        main_agent_version: JsonObject = {"main_agent_version_id": self._current_agent_version_id(), "captured_at": utc_now()}
        redaction_report: JsonObject = {
            "enabled": not self.enable_debug_evidence,
            "policy": "debug-evidence-raw-v1" if self.enable_debug_evidence else "security-redaction-v1",
            "redacted_fields": list(SENSITIVE_KEY_PARTS),
        }
        files = self._build_evidence_files(context, main_agent_version, redaction_report)
        included_files = self._included_evidence_files(files)
        manifest = self._build_evidence_manifest(
            evidence_id=evidence_id,
            feedback_case_id=feedback_case_id,
            feedback_case=feedback_case,
            context=context,
            main_agent_version=main_agent_version,
            redaction_report=redaction_report,
            included_files=included_files,
        )
        with self.Session.begin() as db:
            self._store_evidence_package_rows(
                db,
                manifest=manifest,
                files=files,
            )
            if not self._append_case_update_row(
                db,
                feedback_case,
                evidence_package_id=evidence_id,
                status="pending_attribution",
            ):
                raise RuntimeError("Feedback case disappeared during evidence package creation.")
        return manifest

    def _collect_evidence_context(self, feedback_case: JsonObject) -> JsonObject:
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
        return {
            "signals_clean": signals_clean,
            "events_clean": events_clean,
            "runs_clean": runs_clean,
            "sessions": sessions,
            "tool_calls": tool_calls,
            "messages": messages,
            "agent_activity": agent_activity,
            "langfuse_trace_refs": langfuse_trace_refs,
            "trace_summary": trace_summary,
        }

    def _build_evidence_files(
        self,
        context: JsonObject,
        main_agent_version: JsonObject,
        redaction_report: JsonObject,
    ) -> JsonObject:
        files: JsonObject = {
            "feedback.json": context["signals_clean"],
            "runs.json": context["runs_clean"],
            "sessions.json": context["sessions"],
            "tool_calls.json": context["tool_calls"],
            "soc_events.json": context["events_clean"],
            "trace_summary.json": context["trace_summary"],
            "main_agent_version.json": main_agent_version,
            "redaction_report.json": redaction_report,
        }
        if self.enable_debug_evidence:
            files.update(
                {
                    "messages.json": context["messages"],
                    "agent_activity.json": context["agent_activity"],
                    "langfuse_trace_refs.json": context["langfuse_trace_refs"],
                }
            )
        return files

    def _included_evidence_files(self, files: JsonObject) -> list[JsonObject]:
        return [
            EvidenceIncludedFileRecord(
                path=name,
                sha256=self._sha256_json(self._evidence_payload(payload)),
                type=name.removesuffix(".json"),
            ).to_payload()
            for name, payload in files.items()
        ]

    def _build_evidence_manifest(
        self,
        *,
        evidence_id: str,
        feedback_case_id: str,
        feedback_case: JsonObject,
        context: JsonObject,
        main_agent_version: JsonObject,
        redaction_report: JsonObject,
        included_files: list[JsonObject],
    ) -> JsonObject:
        trace_ids = self._unique_strings([item.get("trace_id") for item in context["langfuse_trace_refs"]])
        record = EvidencePackageRecord.model_validate(
            {
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
                    "has_feedback": bool(context["signals_clean"]),
                    "has_runs": bool(context["runs_clean"]),
                    "has_tool_calls": bool(context["tool_calls"]),
                    "has_trace_summary": bool(context["trace_summary"]),
                    "has_main_agent_version": bool(main_agent_version["main_agent_version_id"]),
                    "has_messages": bool(context["messages"] and any(item.get("messages") for item in context["messages"])),
                    "has_agent_activity": bool(context["agent_activity"] and any(item.get("agent_activity") for item in context["agent_activity"])),
                    "has_langfuse_trace_refs": bool(context["langfuse_trace_refs"]),
                    "has_langfuse_trace_details": False,
                },
            }
        )
        return record.to_payload()

    def _store_evidence_package_rows(
        self,
        db: Any,
        *,
        manifest: JsonObject,
        files: JsonObject,
    ) -> None:
        record = EvidencePackageRecord.model_validate(manifest)
        db.add(
            EvidencePackageModel(
                evidence_package_id=record.evidence_package_id,
                feedback_case_id=record.feedback_case_id,
                created_at=record.created_at,
                manifest_json=record.to_payload(),
            )
        )
        db.flush()
        for item in record.included_files:
            content = self._evidence_payload(files[item.path])
            db.add(
                EvidenceFileModel(
                    evidence_package_id=record.evidence_package_id,
                    file_name=item.path,
                    file_type=item.type,
                    sha256=item.sha256,
                    content_json=content,
                )
            )

    def get_evidence_package(self, evidence_package_id: str) -> Optional[JsonObject]:
        if not evidence_package_id:
            return None
        with self.Session() as db:
            record = db.get(EvidencePackageModel, evidence_package_id)
            return EvidencePackageRecord.from_row(record).to_payload() if record else None

    def get_evidence_package_file(self, evidence_package_id: str, file_name: str) -> Optional[JsonObject]:
        if not file_name or Path(file_name).name != file_name or file_name == "manifest.json":
            return None
        with self.Session() as db:
            record = db.get(EvidenceFileModel, {"evidence_package_id": evidence_package_id, "file_name": file_name})
            if not record:
                return None
            return EvidencePackageFileRecord.from_row(record).to_payload()

    def _evidence_payload(self, value: Any) -> Any:
        if self.enable_debug_evidence:
            return value
        return self._scrub_record(value)

    def _langfuse_trace_refs(self, runs: list[JsonObject]) -> list[JsonObject]:
        refs: list[JsonObject] = []
        for run in runs:
            trace_id = self._string(run.get("langfuse_trace_id"))
            trace_url = self._string(run.get("langfuse_trace_url"))
            if not trace_id and not trace_url:
                continue
            refs.append({"run_id": run.get("run_id"), "session_id": run.get("session_id"), "trace_id": trace_id, "trace_url": trace_url})
        return refs

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
