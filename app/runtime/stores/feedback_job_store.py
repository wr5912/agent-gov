from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, select

from ..agent_profiles import ATTRIBUTION_ANALYZER_PROFILE, PROPOSAL_GENERATOR_PROFILE
from ..feedback_job_flags import with_reused_existing
from ..feedback_schemas import validate_attribution_output, validate_proposal_output
from ..runtime_db import (
    ExternalGovernanceItemModel,
    ExternalNotificationModel,
    FeedbackCaseModel,
    FeedbackJobModel,
    OptimizationProposalModel,
    ProposalReviewModel,
    utc_now,
)
from ..state_machines import JOB_IN_PROGRESS_STATES, validate_transition


_UNSET = object()


class FeedbackJobStoreMixin:
    """Store operations for feedback-loop agent jobs and job artifacts."""

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

    def create_attribution_job(
        self,
        feedback_case_id: str,
        *,
        evidence_package_id: Optional[str] = None,
        profile_version: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        with self._job_create_lock:
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
                return with_reused_existing(existing)
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
            try:
                self.feedback_jobs.create_queued_job(
                    job_id=job_id,
                    job_type="attribution",
                    feedback_case_id=feedback_case_id,
                    evidence_package_id=evidence_package_id,
                    profile_name=ATTRIBUTION_ANALYZER_PROFILE,
                    input_payload=input_payload,
                    profile_version=profile_version,
                )
                self._append_case_update(feedback_case, attribution_job_id=job_id, status="attribution_queued")
            except Exception:
                self._discard_job(job_id)
                raise
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
            return with_reused_existing(existing)
        evidence_package_id = evidence_package_id or self._latest(feedback_case.get("evidence_package_ids"))
        attribution_job_id = attribution_job_id or self._latest(feedback_case.get("attribution_job_ids"))
        if not evidence_package_id or not attribution_job_id:
            return None
        attribution_output = self.get_job_output(attribution_job_id, "attribution")
        if not attribution_output:
            return None

        job_id = f"fbp-{uuid.uuid4()}"
        try:
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
            self.feedback_jobs.create_queued_job(
                job_id=job_id,
                job_type="proposal",
                feedback_case_id=feedback_case_id,
                evidence_package_id=evidence_package_id,
                profile_name=PROPOSAL_GENERATOR_PROFILE,
                input_payload=input_payload,
                profile_version=profile_version,
                attribution_job_id=attribution_job_id,
            )
            if force:
                self._supersede_case_proposals(
                    feedback_case_id,
                    reason="proposal_regenerated",
                    superseded_by_job_id=job_id,
                )
            self._append_case_update(feedback_case, proposal_job_id=job_id, status="proposal_queued")
        except Exception:
            self._discard_job(job_id)
            raise
        return self.get_job(job_id)

    def revalidate_proposal_job(self, job_id: str) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job or job.get("job_type") != "proposal":
            return None
        raw_output = job.get("raw_output_json")
        if not isinstance(raw_output, dict):
            return None
        validated, error = validate_proposal_output(raw_output)
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        if not validated:
            error_payload = self._job_error_payload(job, "SCHEMA_VALIDATION_FAILED", error or "invalid proposal output")
            with self.Session.begin() as db:
                if not self._set_job_json_row(db, job_id, error_json=error_payload):
                    return None
                self._append_job_update_row(db, job_id, status="schema_validating")
                self._append_job_update_row(db, job_id, status="needs_human_review", completed_at=utc_now())
                if feedback_case:
                    self._append_case_update_row(db, feedback_case, status="needs_human_review")
            self._cleanup_job_tmp(job_id)
            return self.get_job(job_id)

        normalized = self._normalize_proposal_output(validated, job)
        with self.Session.begin() as db:
            if not self._set_job_json_row(db, job_id):
                return None
            normalized["external_guidance"] = self._upsert_external_governance_items_rows(db, normalized, job)
            self._set_job_json_row(db, job_id, validated_output_json=normalized, error_json=None)
            self._append_job_update_row(db, job_id, status="schema_validating")
            self._append_job_update_row(db, job_id, status="completed", completed_at=utc_now())
            for proposal in normalized.get("proposals", []):
                db.merge(self._proposal_model_from_dict(proposal))
            if feedback_case:
                self._append_case_update_row(
                    db,
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
        validated, error = validate_attribution_output(raw_output)
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        if not validated:
            error_payload = self._job_error_payload(job, "SCHEMA_VALIDATION_FAILED", error or "invalid attribution output")
            with self.Session.begin() as db:
                if not self._set_job_json_row(db, job_id, raw_output_json=raw_output, error_json=error_payload):
                    return None
                self._append_job_update_row(db, job_id, status="schema_validating")
                self._append_job_update_row(db, job_id, status="needs_human_review", completed_at=utc_now())
                if feedback_case:
                    self._append_case_update_row(db, feedback_case, status="needs_human_review")
            self._cleanup_job_tmp(job_id)
            return self.get_job(job_id)
        with self.Session.begin() as db:
            if not self._set_job_json_row(db, job_id, raw_output_json=raw_output, validated_output_json=validated, error_json=None):
                return None
            self._append_job_update_row(db, job_id, status="schema_validating")
            self._append_job_update_row(db, job_id, status="completed", completed_at=utc_now())
            if feedback_case:
                self._append_case_update_row(db, feedback_case, status="pending_proposal")
        self._cleanup_job_tmp(job_id)
        return self.get_job(job_id)

    def complete_proposal_job(self, job_id: str, raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return None
        validated, error = validate_proposal_output(raw_output)
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        if not validated:
            error_payload = self._job_error_payload(job, "SCHEMA_VALIDATION_FAILED", error or "invalid proposal output")
            with self.Session.begin() as db:
                if not self._set_job_json_row(db, job_id, raw_output_json=raw_output, error_json=error_payload):
                    return None
                self._append_job_update_row(db, job_id, status="schema_validating")
                self._append_job_update_row(db, job_id, status="needs_human_review", completed_at=utc_now())
                if feedback_case:
                    self._append_case_update_row(db, feedback_case, status="needs_human_review")
            self._cleanup_job_tmp(job_id)
            return self.get_job(job_id)

        normalized = self._normalize_proposal_output(validated, job)
        with self.Session.begin() as db:
            if not self._set_job_json_row(db, job_id, raw_output_json=raw_output):
                return None
            normalized["external_guidance"] = self._upsert_external_governance_items_rows(db, normalized, job)
            self._set_job_json_row(db, job_id, validated_output_json=normalized, error_json=None)
            self._append_job_update_row(db, job_id, status="schema_validating")
            self._append_job_update_row(db, job_id, status="completed", completed_at=utc_now())
            for proposal in normalized.get("proposals", []):
                db.merge(self._proposal_model_from_dict(proposal))
            if feedback_case:
                self._append_case_update_row(
                    db,
                    feedback_case,
                    status="pending_review" if normalized.get("proposals") else "needs_human_review",
                )
        self._cleanup_job_tmp(job_id)
        return self.get_job(job_id)

    def fail_job(self, job_id: str, *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return None
        error_payload = self._job_error_payload(job, error_code, message)
        feedback_case = self.find_case(str(job["feedback_case_id"]))
        with self.Session.begin() as db:
            if not self._set_job_json_row(db, job_id, error_json=error_payload):
                return None
            failed_row = self._append_job_update_row(db, job_id, status="failed", completed_at=utc_now())
            failed = self._job_to_dict(failed_row) if failed_row else None
            if feedback_case:
                if job.get("job_type") == "attribution":
                    self._append_case_update_row(db, feedback_case, status="pending_attribution")
                elif job.get("job_type") == "proposal":
                    self._append_case_update_row(db, feedback_case, status="pending_proposal")
            if job.get("job_type") == "batch_plan":
                batch_id = self._job_batch_id(job)
                if batch_id:
                    self._update_batch_row(
                        db,
                        batch_id,
                        status="needs_human_review",
                        fields={
                            "optimization_plan_job_id": job_id,
                            "optimization_plan_job": failed,
                            "optimization_plan_error": (failed or {}).get("error_json"),
                        },
                    )
        self._cleanup_job_tmp(job_id)
        return self.get_job(job_id)

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
        cleanup_job_ids: list[str] = []
        with self.Session.begin() as db:
            if not self._discard_current_attribution_row(db, feedback_case_id, invalidate_downstream=invalidate_downstream, cleanup_job_ids=cleanup_job_ids):
                return None
        for job_id in cleanup_job_ids:
            self._cleanup_job_tmp(job_id)
        return self.find_case(feedback_case_id)

    def _discard_current_attribution_row(
        self,
        db: Any,
        feedback_case_id: str,
        *,
        invalidate_downstream: bool,
        cleanup_job_ids: list[str],
    ) -> bool:
        row = db.get(FeedbackCaseModel, feedback_case_id)
        if not row:
            return False
        attribution_job_id = self._string(row.current_attribution_job_id)
        proposal_job_id = self._string(row.current_proposal_job_id) if invalidate_downstream else None
        if attribution_job_id and self._discard_job_row(db, attribution_job_id):
            cleanup_job_ids.append(attribution_job_id)
        if proposal_job_id and self._discard_proposal_job_row(db, proposal_job_id):
            cleanup_job_ids.append(proposal_job_id)
        row.updated_at = utc_now()
        validate_transition("case", row.status, "pending_attribution")
        row.status = "pending_attribution"
        row.current_attribution_job_id = None
        if invalidate_downstream:
            row.current_proposal_job_id = None
        return True


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
            if not self._append_job_update_row(
                db,
                job_id,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
            ):
                return None
        return self.get_job(job_id)

    def _append_job_update_row(
        self,
        db: Any,
        job_id: str,
        *,
        status: str,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> Optional[FeedbackJobModel]:
        job = db.get(FeedbackJobModel, job_id)
        if not job:
            return None
        validate_transition("job", job.status, status)
        job.status = status
        if started_at is not None:
            job.started_at = started_at
        if completed_at is not None:
            job.completed_at = completed_at
        return job

    def _set_job_json(
        self,
        job_id: str,
        *,
        raw_output_json: Optional[dict[str, Any]] = None,
        validated_output_json: Optional[dict[str, Any]] = None,
        error_json: Optional[dict[str, Any]] = None,
    ) -> None:
        with self.Session.begin() as db:
            self._set_job_json_row(
                db,
                job_id,
                raw_output_json=raw_output_json if raw_output_json is not None else _UNSET,
                validated_output_json=validated_output_json if validated_output_json is not None else _UNSET,
                error_json=error_json if error_json is not None else _UNSET,
            )

    def _set_job_json_row(
        self,
        db: Any,
        job_id: str,
        *,
        raw_output_json: Any = _UNSET,
        validated_output_json: Any = _UNSET,
        error_json: Any = _UNSET,
    ) -> Optional[FeedbackJobModel]:
        job = db.get(FeedbackJobModel, job_id)
        if not job:
            return None
        if raw_output_json is not _UNSET:
            job.raw_output_json = raw_output_json
        if validated_output_json is not _UNSET:
            job.validated_output_json = validated_output_json
        if error_json is not _UNSET:
            job.error_json = error_json
        return job

    def _write_job_error(self, job: dict[str, Any], error_code: str, message: str) -> None:
        error_payload = self._job_error_payload(job, error_code, message)
        self._set_job_json(
            job["job_id"],
            error_json=error_payload,
        )

    def _job_error_payload(self, job: dict[str, Any], error_code: str, message: str) -> dict[str, Any]:
        error_payload: dict[str, Any] = {"error_code": error_code, "message": message, "created_at": utc_now(), "job_id": job["job_id"]}
        return self._normalize_job_error_payload(error_payload)

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
        if job.get("status") not in JOB_IN_PROGRESS_STATES:
            return False
        base = self._parse_datetime(self._string(job.get("started_at")) or self._string(job.get("created_at")))
        if not base:
            return False
        timeout_seconds = int(job.get("timeout_seconds") or 300)
        return datetime.now(timezone.utc) >= base + timedelta(seconds=timeout_seconds)


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
        cleanup = False
        with self.Session.begin() as db:
            cleanup = self._discard_job_row(db, job_id)
        if cleanup:
            self._cleanup_job_tmp(job_id)

    def _discard_job_row(self, db: Any, job_id: str) -> bool:
        row = db.get(FeedbackJobModel, job_id)
        if not row:
            return False
        db.delete(row)
        return True

    def _discard_proposal_job(self, proposal_job_id: str) -> None:
        if not proposal_job_id:
            return
        cleanup = False
        with self.Session.begin() as db:
            cleanup = self._discard_proposal_job_row(db, proposal_job_id)
        if cleanup:
            self._cleanup_job_tmp(proposal_job_id)

    def _discard_proposal_job_row(self, db: Any, proposal_job_id: str) -> bool:
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
        if not row:
            return bool(proposal_ids or external_items)
        db.delete(row)
        return True
