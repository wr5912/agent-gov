from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from sqlalchemy import select, update

from ..agent_job_logging import log_agent_job_event
from ..agent_job_types import AgentJobType, FormatterOutputModel, ProjectedOutputModel, coerce_agent_job_type
from ..feedback_schemas import (
    AttributionFormatterOutput,
    ExecutionPlanFormatterOutput,
    ExecutionPlanOutput,
    FeedbackEvalCaseGenerationFormatterOutput,
    FeedbackOptimizationPlanFormatterOutput,
    RegressionImpactAnalysisFormatterOutput,
    coerce_feedback_eval_case_generation_output_model,
    coerce_regression_impact_analysis_output_model,
    output_model_payload,
)
from ..json_types import JsonObject
from ..records.agent_job_records import AgentJobRecord
from ..records.regression_impact_records import RegressionImpactAnalysisRecord, apply_regression_impact_analysis_record
from ..runtime_db import (
    AgentJobModel,
    FeedbackOptimizationBatchModel,
    RegressionImpactAnalysisModel,
    utc_now,
)

_UNSET = object()
logger = logging.getLogger(__name__)


class AgentJobStoreMixin:
    """Generic async Agent job queue and domain projection helpers."""

    def create_agent_job(
        self,
        *,
        job_id: str,
        job_type: str,
        scope_kind: str,
        scope_id: str,
        profile_name: str,
        input_payload: JsonObject,
        input_path: Optional[str] = None,
        profile_version: Optional[JsonObject] = None,
        status: str = "queued",
    ) -> JsonObject:
        input_path = input_path or self._write_agent_job_input(job_id, job_type, input_payload)
        now = utc_now()
        row = AgentJobModel(
            job_id=job_id,
            job_type=job_type,
            scope_kind=scope_kind,
            scope_id=scope_id,
            status=status,
            profile_name=profile_name,
            created_at=now,
            started_at=None,
            completed_at=now if status in {"completed", "failed", "needs_human_review"} else None,
            input_path=input_path,
            raw_output_path=f"sqlite://agent_jobs/{job_id}/raw_output_json",
            validated_output_path=f"sqlite://agent_jobs/{job_id}/validated_output_json",
            error_path=f"sqlite://agent_jobs/{job_id}/error_json",
            runtime_version=self.runtime_version,
            schema_version=f"{job_type}-agent-job/v1",
            timeout_seconds=300,
            retry_count=0,
            profile_version_json=profile_version,
            input_json=input_payload,
        )
        with self.Session.begin() as db:
            existing = db.get(AgentJobModel, job_id)
            if existing:
                return self._agent_job_to_dict(existing)
            db.add(row)
        created = self.get_agent_job(job_id) or self._agent_job_to_dict(row)
        log_agent_job_event(logger, logging.INFO, "agent_job.queued", created)
        return created

    def get_agent_job(self, job_id: str) -> Optional[JsonObject]:
        if not job_id:
            return None
        with self.Session() as db:
            row = db.get(AgentJobModel, job_id)
            return self._agent_job_to_dict(row) if row else None

    def list_agent_jobs(
        self,
        *,
        job_type: Optional[str] = None,
        scope_kind: Optional[str] = None,
        scope_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(AgentJobModel).order_by(AgentJobModel.created_at.desc()).limit(limit)
        if job_type:
            stmt = stmt.where(AgentJobModel.job_type == job_type)
        if scope_kind:
            stmt = stmt.where(AgentJobModel.scope_kind == scope_kind)
        if scope_id:
            stmt = stmt.where(AgentJobModel.scope_id == scope_id)
        if status:
            stmt = stmt.where(AgentJobModel.status == status)
        with self.Session() as db:
            return [self._agent_job_to_dict(row) for row in db.scalars(stmt).all()]

    def claim_next_agent_job(self, *, job_types: Optional[list[str]] = None) -> Optional[JsonObject]:
        now = utc_now()
        with self.Session.begin() as db:
            stmt = select(AgentJobModel).where(AgentJobModel.status == "queued").order_by(AgentJobModel.created_at.asc()).limit(20)
            if job_types:
                stmt = stmt.where(AgentJobModel.job_type.in_(job_types))
            for candidate in db.scalars(stmt).all():
                AgentJobRecord.from_row(candidate).transition_to("running", started_at=now)
                result = db.execute(
                    update(AgentJobModel)
                    .where(AgentJobModel.job_id == candidate.job_id, AgentJobModel.status == "queued")
                    .values(status="running", started_at=now)
                )
                if result.rowcount != 1:
                    continue
                db.flush()
                row = db.get(AgentJobModel, candidate.job_id)
                return self._agent_job_to_dict(row) if row else None
        return None

    def _timeout_stale_agent_jobs(self, *, limit: int = 100) -> list[JsonObject]:
        now = utc_now()
        now_dt = datetime.now(timezone.utc)
        timed_out_ids: list[str] = []
        with self.Session.begin() as db:
            stmt = (
                select(AgentJobModel)
                .where(AgentJobModel.status.in_(("running", "evidence_packaging", "schema_validating")))
                .order_by(AgentJobModel.started_at.asc(), AgentJobModel.created_at.asc())
                .limit(limit)
            )
            for row in db.scalars(stmt).all():
                base = self._parse_datetime(row.started_at or row.created_at)
                if not base:
                    continue
                timeout_seconds = int(row.timeout_seconds or 300)
                if now_dt < base + timedelta(seconds=timeout_seconds):
                    continue
                error_payload = {
                    "error_code": "AGENT_TIMEOUT",
                    "message": f"Agent job exceeded timeout_seconds={timeout_seconds}",
                    "created_at": now,
                    "job_id": row.job_id,
                }
                if not self._set_agent_job_json_row(db, row.job_id, error_json=error_payload):
                    continue
                if not self._append_agent_job_update_row(db, row.job_id, status="timeout", completed_at=now):
                    continue
                timed_out_ids.append(row.job_id)
        timed_out = [job for job in (self.get_agent_job(job_id) for job_id in timed_out_ids) if job]
        for job in timed_out:
            if job.get("job_type") == AgentJobType.ATTRIBUTION:
                self._sync_attribution_agent_job_to_batches(job, job)
            elif job.get("job_type") == AgentJobType.BATCH_PLAN:
                self._sync_batch_plan_agent_job_to_batch(job)
        return timed_out

    def complete_projected_agent_job(
        self,
        job: JsonObject,
        job_output: FormatterOutputModel | ProjectedOutputModel | JsonObject,
    ) -> Optional[JsonObject]:
        job_id = str(job.get("job_id") or "")
        try:
            job_type = coerce_agent_job_type(str(job.get("job_type") or ""))
        except ValueError:
            return self.fail_agent_job(job_id, error_code="UNSUPPORTED_AGENT_JOB_TYPE", message=f"Unsupported agent job type: {job.get('job_type')}")
        if job_type == AgentJobType.ATTRIBUTION:
            projected = self.complete_attribution_job(
                job_id,
                cast(AttributionFormatterOutput | JsonObject, job_output),
            )
            self._sync_attribution_agent_job_to_batches(job, projected)
            return projected
        if job_type == AgentJobType.BATCH_PLAN:
            return self.complete_batch_plan_job(
                job_id,
                cast(FeedbackOptimizationPlanFormatterOutput | JsonObject, job_output),
            )
        if job_type == AgentJobType.EXECUTION:
            return self.complete_execution_job(
                job_id,
                cast(ExecutionPlanFormatterOutput | ExecutionPlanOutput | JsonObject, job_output),
            )
        if job_type == AgentJobType.EVAL_CASE_GENERATION:
            return self._complete_eval_case_generation_agent_job(
                job,
                cast(FeedbackEvalCaseGenerationFormatterOutput | JsonObject, job_output),
            )
        if job_type == AgentJobType.REGRESSION_IMPACT_ANALYSIS:
            return self._complete_regression_impact_agent_job(
                job,
                cast(RegressionImpactAnalysisFormatterOutput | JsonObject, job_output),
            )
        return self.fail_agent_job(job_id, error_code="UNSUPPORTED_AGENT_JOB_TYPE", message=f"Unsupported agent job type: {job_type}")

    def fail_projected_agent_job(
        self,
        job: JsonObject,
        *,
        error_code: str,
        message: str,
        raw_output_json: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        job_id = str(job.get("job_id") or "")
        try:
            job_type = coerce_agent_job_type(str(job.get("job_type") or ""))
        except ValueError:
            return self.fail_agent_job(
                job_id, error_code="UNSUPPORTED_AGENT_JOB_TYPE", message=f"Unsupported agent job type: {job.get('job_type')}", raw_output_json=raw_output_json
            )
        if job_type in {AgentJobType.ATTRIBUTION, AgentJobType.BATCH_PLAN}:
            failed = self.fail_job(job_id, error_code=error_code, message=message, raw_output_json=raw_output_json)
            if job_type == AgentJobType.ATTRIBUTION:
                self._sync_attribution_agent_job_to_batches(job, failed)
            return failed
        elif job_type == AgentJobType.EXECUTION:
            return self.fail_execution_job(job_id, error_code=error_code, message=message, raw_output_json=raw_output_json)
        elif job_type == AgentJobType.REGRESSION_IMPACT_ANALYSIS:
            self._fail_regression_impact_projection(job, error_code=error_code, message=message)
        return self.fail_agent_job(job_id, error_code=error_code, message=message, raw_output_json=raw_output_json)

    def fail_agent_job(
        self,
        job_id: str,
        *,
        error_code: str,
        message: str,
        raw_output_json: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        error_payload = {"error_code": error_code, "message": message, "created_at": utc_now(), "job_id": job_id}
        with self.Session.begin() as db:
            row = self._set_agent_job_json_row(
                db,
                job_id,
                raw_output_json=raw_output_json if raw_output_json is not None else _UNSET,
                error_json=error_payload,
            )
            if not row:
                return None
            self._append_agent_job_update_row(db, job_id, status="failed", completed_at=utc_now())
        return self.get_agent_job(job_id)

    def _complete_agent_job_from_domain(
        self,
        job_id: str,
        projected: Optional[JsonObject],
        *,
        ready_status: str = "completed",
    ) -> Optional[JsonObject]:
        if not projected:
            return self.fail_agent_job(job_id, error_code="DOMAIN_PROJECTION_FAILED", message="Agent job domain projection failed")
        domain_status = str(projected.get("status") or "")
        target_status = "completed" if domain_status in {"completed", ready_status} else domain_status
        if target_status not in {"completed", "needs_human_review", "failed"}:
            target_status = "completed"
        return self._complete_agent_job(
            job_id,
            raw_output_json=projected.get("raw_output_json"),
            validated_output_json=projected.get("validated_output_json"),
            error_json=projected.get("error_json"),
            status=target_status,
        )

    def _complete_agent_job(
        self,
        job_id: str,
        *,
        raw_output_json: Any = _UNSET,
        validated_output_json: Any = _UNSET,
        error_json: Any = _UNSET,
        status: str,
    ) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            row = self._set_agent_job_json_row(
                db,
                job_id,
                raw_output_json=raw_output_json,
                validated_output_json=validated_output_json,
                error_json=error_json,
            )
            if not row:
                return None
            self._append_agent_job_update_row(db, job_id, status="schema_validating")
            self._append_agent_job_update_row(db, job_id, status=status, completed_at=utc_now())
        return self.get_agent_job(job_id)

    def _complete_eval_case_generation_agent_job(
        self,
        job: JsonObject,
        formatter_output: FeedbackEvalCaseGenerationFormatterOutput | JsonObject,
    ) -> Optional[JsonObject]:
        output_model, error = coerce_feedback_eval_case_generation_output_model(formatter_output)
        if output_model:
            raw_payload = output_model_payload(output_model)
        elif isinstance(formatter_output, FeedbackEvalCaseGenerationFormatterOutput):
            raw_payload = output_model_payload(formatter_output)
        else:
            raw_payload = formatter_output
        if not output_model:
            return self._complete_agent_job(
                str(job["job_id"]),
                raw_output_json=raw_payload,
                error_json={"error_code": "SCHEMA_VALIDATION_FAILED", "message": error or "invalid eval case generation output"},
                status="needs_human_review",
            )
        validated = output_model_payload(output_model)
        projected = self._project_eval_case_generation(job, validated)
        return self._complete_agent_job(
            str(job["job_id"]),
            raw_output_json=raw_payload,
            validated_output_json=projected,
            error_json=None,
            status="completed" if projected.get("status") == "completed" else "needs_human_review",
        )

    def _complete_regression_impact_agent_job(
        self,
        job: JsonObject,
        formatter_output: RegressionImpactAnalysisFormatterOutput | JsonObject,
    ) -> Optional[JsonObject]:
        formatter_payload = (
            output_model_payload(formatter_output) if isinstance(formatter_output, RegressionImpactAnalysisFormatterOutput) else dict(formatter_output)
        )
        output = dict(formatter_payload)
        output["eval_run_id"] = job.get("scope_id")
        output_model, error = coerce_regression_impact_analysis_output_model(output)
        raw_payload = output_model_payload(output_model) if output_model else formatter_payload
        if not output_model:
            self._fail_regression_impact_projection(job, error_code="SCHEMA_VALIDATION_FAILED", message=error or "invalid impact output")
            return self._complete_agent_job(
                str(job["job_id"]),
                raw_output_json=raw_payload,
                error_json={"error_code": "SCHEMA_VALIDATION_FAILED", "message": error or "invalid impact output"},
                status="needs_human_review",
            )
        validated = output_model_payload(output_model)
        projected = self._project_regression_impact(job, validated)
        return self._complete_agent_job(
            str(job["job_id"]),
            raw_output_json=raw_payload,
            validated_output_json=projected,
            error_json=None,
            status="completed" if projected.get("status") == "completed" else "needs_human_review",
        )

    def _sync_attribution_agent_job_to_batches(self, job: JsonObject, projected: Optional[JsonObject]) -> None:
        if not projected:
            return
        feedback_case_id = self._string(projected.get("feedback_case_id")) or self._string(job.get("scope_id"))
        if not feedback_case_id:
            return
        for batch in self.list_optimization_batches(limit=500):
            if feedback_case_id not in set(batch.get("feedback_case_ids") or []):
                continue
            job_ids = self._unique_strings(batch.get("attribution_job_ids") or [])
            if not job_ids:
                for case_id in batch.get("feedback_case_ids") or []:
                    case = self.find_case(str(case_id))
                    latest_job_id = self._latest((case or {}).get("attribution_job_ids"))
                    if latest_job_id:
                        job_ids.append(str(latest_job_id))
            if job.get("job_id") not in set(job_ids):
                continue
            jobs = [domain_job for domain_job in (self.get_job(str(job_id)) for job_id in job_ids) if domain_job]
            if jobs:
                self.record_batch_attribution_jobs(str(batch["batch_id"]), jobs)

    def _sync_batch_plan_agent_job_to_batch(self, job: JsonObject) -> None:
        batch_id = self._batch_plan_job_batch_id(job)
        if not batch_id:
            return
        with self.Session.begin() as db:
            self._update_batch_row(
                db,
                batch_id,
                status="needs_human_review",
                fields={
                    "optimization_plan_job_id": job["job_id"],
                    "optimization_plan_job": job,
                    "optimization_plan_error": job.get("error_json"),
                },
            )

    def _batch_plan_job_batch_id(self, job: JsonObject) -> Optional[str]:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        return self._string(input_json.get("batch_id")) or (self._string(job.get("scope_id")) if job.get("scope_kind") == "optimization_batch" else None)

    def _project_eval_case_generation(self, job: JsonObject, output: JsonObject) -> JsonObject:
        job_input = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        force = bool(job_input.get("force"))
        created = reused = updated = skipped = 0
        eval_cases: list[JsonObject] = []
        results: list[JsonObject] = []
        now = utc_now()
        with self.Session.begin() as db:
            for item in output.get("eval_cases") or []:
                if not isinstance(item, dict) or not self._string(item.get("prompt")):
                    skipped += 1
                    results.append({"status": "skipped", "reason": "missing prompt"})
                    continue
                payload = self._eval_case_payload_from_agent(item, job_input, now)
                if payload is None:
                    skipped += 1
                    results.append({"status": "skipped", "reason": "source feedback case is not in job input"})
                    continue
                existing = self.find_eval_case(source_feedback_case_id=self._string(payload.get("source_feedback_case_id")))
                if existing and not force:
                    reused += 1
                    eval_cases.append(existing)
                    results.append(self._eval_case_generation_result(payload, existing, "reused"))
                    continue
                if existing:
                    payload["eval_case_id"] = existing["eval_case_id"]
                    payload["created_at"] = existing["created_at"]
                    self._update_eval_case_row(db, payload)
                    updated += 1
                    eval_cases.append(payload)
                    results.append(self._eval_case_generation_result(payload, payload, "updated"))
                    continue
                self._add_eval_case_row(db, payload)
                created += 1
                eval_cases.append(payload)
                results.append(self._eval_case_generation_result(payload, payload, "created"))
            self._sync_eval_generation_scope_row(db, job, eval_cases, created, reused, updated, skipped, results)
        return {
            **output,
            "job_id": job["job_id"],
            "scope_kind": job.get("scope_kind"),
            "scope_id": job.get("scope_id"),
            "status": "completed" if eval_cases else "needs_human_review",
            "created": created,
            "reused": reused,
            "updated": updated,
            "skipped": skipped,
            "eval_cases": eval_cases,
            "results": results,
        }

    def _project_regression_impact(self, job: JsonObject, output: JsonObject) -> JsonObject:
        eval_run_id = str(job.get("scope_id") or "")
        eval_run = self.get_eval_run(eval_run_id) or {}
        completed_at = utc_now()
        with self.Session.begin() as db:
            row = db.scalars(select(RegressionImpactAnalysisModel).where(RegressionImpactAnalysisModel.eval_run_id == eval_run_id)).first()
            impact_analysis_id = row.impact_analysis_id if row else f"ria-{uuid.uuid4()}"
            record_fields = set(RegressionImpactAnalysisRecord.model_fields)
            recommendations = self._string_list(output.get("recommendations")) or self._impact_recommendations(eval_run)
            payload = {
                **{key: value for key, value in output.items() if key in record_fields},
                "impact_analysis_id": impact_analysis_id,
                "eval_run_id": eval_run_id,
                "created_at": row.created_at if row else utc_now(),
                "completed_at": completed_at,
                "status": output.get("status") or "completed",
                "job_id": job["job_id"],
                "result_status": eval_run.get("result_status"),
                "gate_result": eval_run.get("gate_result") if isinstance(eval_run.get("gate_result"), dict) else {},
                "impacted_assets": self._impacted_assets_from_eval_run(eval_run),
                "recommendations": recommendations,
                "error_json": None,
            }
            record = (
                RegressionImpactAnalysisRecord.from_row(row).transition_to(str(payload["status"]), fields=payload)
                if row
                else RegressionImpactAnalysisRecord.model_validate(payload)
            )
            if row:
                apply_regression_impact_analysis_record(row, record)
            else:
                db.add(
                    RegressionImpactAnalysisModel(
                        impact_analysis_id=record.impact_analysis_id,
                        eval_run_id=record.eval_run_id,
                        created_at=record.created_at,
                        completed_at=record.completed_at,
                        status=record.status,
                        job_id=record.job_id,
                        payload_json=record.to_payload(),
                    )
                )
        return self.get_regression_impact_analysis(eval_run_id) or payload

    def _fail_regression_impact_projection(self, job: JsonObject, *, error_code: str, message: str) -> None:
        eval_run_id = str(job.get("scope_id") or "")
        with self.Session.begin() as db:
            row = db.scalars(select(RegressionImpactAnalysisModel).where(RegressionImpactAnalysisModel.eval_run_id == eval_run_id)).first()
            if not row:
                return
            record = RegressionImpactAnalysisRecord.from_row(row).transition_to(
                "failed",
                fields={
                    "completed_at": utc_now(),
                    "job_id": job.get("job_id"),
                    "error_json": {"error_code": error_code, "message": message, "created_at": utc_now()},
                },
            )
            apply_regression_impact_analysis_record(row, record)

    def _eval_case_payload_from_agent(self, item: JsonObject, job_input: JsonObject, now: str) -> Optional[JsonObject]:
        feedback_contexts = [context for context in job_input.get("feedback_cases") or [] if isinstance(context, dict)]
        feedback_context_by_case_id = {
            str((context.get("feedback_case") or {}).get("feedback_case_id")): context
            for context in feedback_contexts
            if isinstance(context.get("feedback_case"), dict) and (context.get("feedback_case") or {}).get("feedback_case_id")
        }
        allowed_feedback_case_ids = set(feedback_context_by_case_id)
        requested_feedback_case_id = self._string(item.get("source_feedback_case_id"))
        fallback_feedback_case_id = self._string(job_input.get("feedback_case_id"))
        if requested_feedback_case_id and requested_feedback_case_id not in allowed_feedback_case_ids:
            return None
        source_feedback_case_id = requested_feedback_case_id or (fallback_feedback_case_id if fallback_feedback_case_id in allowed_feedback_case_ids else None)
        if not source_feedback_case_id and len(allowed_feedback_case_ids) == 1:
            source_feedback_case_id = next(iter(allowed_feedback_case_ids))
        if not source_feedback_case_id:
            return None

        context = feedback_context_by_case_id.get(source_feedback_case_id) or {}
        source_run = context.get("source_run") if isinstance(context.get("source_run"), dict) else {}
        source_refs = [dict(ref) for ref in context.get("source_refs") or [] if isinstance(ref, dict)]
        if len(source_refs) == 1:
            source_kind = self._string(source_refs[0].get("source_kind")) or "feedback_case"
            source_id = self._string(source_refs[0].get("source_id")) or source_feedback_case_id
        else:
            source_kind = "feedback_case"
            source_id = source_feedback_case_id

        payload = {
            "schema_version": "feedback-eval-case/v1",
            "eval_case_id": f"evc-{uuid.uuid4()}",
            "created_at": now,
            "status": "draft",
            "source": "eval_case_governor",
            "source_feedback_case_id": source_feedback_case_id,
            "source_run_id": self._string(source_run.get("run_id")),
            "source_kind": source_kind,
            "source_id": source_id,
            "source_refs": source_refs,
            "scenario_pack": self._string(item.get("scenario_pack")),
            "prompt": str(item.get("prompt") or "").strip(),
            "expected_behavior": self._string(item.get("expected_behavior")) or "",
            "checks_json": item.get("checks_json") if isinstance(item.get("checks_json"), dict) else {},
            "labels": self._unique_strings([*(item.get("labels") or []), "feedback_optimization"]),
            "source_summary": item.get("source_summary") if isinstance(item.get("source_summary"), dict) else None,
            "attribution_summary": item.get("attribution_summary") if isinstance(item.get("attribution_summary"), dict) else None,
            "proposal_summary": item.get("proposal_summary") if isinstance(item.get("proposal_summary"), dict) else None,
        }
        payload["updated_at"] = now
        return self._eval_case_with_asset_defaults(payload)

    def _eval_case_generation_result(self, payload: JsonObject, eval_case: JsonObject, status: str) -> JsonObject:
        return {
            "source_kind": payload.get("source_kind"),
            "source_id": payload.get("source_id"),
            "feedback_case_id": payload.get("source_feedback_case_id"),
            "eval_case_id": eval_case.get("eval_case_id"),
            "status": status,
        }

    def _sync_eval_generation_scope_row(
        self,
        db: Any,
        job: JsonObject,
        eval_cases: list[JsonObject],
        created: int,
        reused: int,
        updated: int,
        skipped: int,
        results: list[JsonObject],
    ) -> None:
        if job.get("scope_kind") != "optimization_batch":
            return
        batch_id = str(job.get("scope_id") or "")
        row = db.get(FeedbackOptimizationBatchModel, batch_id)
        if not row:
            return
        payload = self._batch_payload_snapshot(row)
        eval_case_ids = self._unique_strings([*(payload.get("eval_case_ids") or []), *[case.get("eval_case_id") for case in eval_cases]])
        self._update_batch_row(
            db,
            batch_id,
            status=row.status,
            fields={
                "eval_case_ids": eval_case_ids,
                "eval_case_generation_job_id": job.get("job_id"),
                "eval_case_generation": {
                    "created": created,
                    "reused": reused,
                    "updated": updated,
                    "skipped": skipped,
                    "eval_cases": eval_cases,
                    "results": results,
                },
            },
        )

    def _set_agent_job_json_row(
        self,
        db: Any,
        job_id: str,
        *,
        raw_output_json: Any = _UNSET,
        validated_output_json: Any = _UNSET,
        error_json: Any = _UNSET,
    ) -> Optional[AgentJobModel]:
        row = db.get(AgentJobModel, job_id)
        if not row:
            return None
        fields: JsonObject = {}
        if raw_output_json is not _UNSET:
            fields["raw_output_json"] = raw_output_json
        if validated_output_json is not _UNSET:
            fields["validated_output_json"] = validated_output_json
        if error_json is not _UNSET:
            fields["error_json"] = error_json
        return self._apply_agent_job_json_fields(row, fields)

    def _apply_agent_job_json_fields(self, row: AgentJobModel, fields: JsonObject) -> AgentJobModel:
        payload = AgentJobRecord.from_row(row).to_payload()
        payload.update(fields)
        record = AgentJobRecord.model_validate(payload)
        row.raw_output_json = record.raw_output_json
        row.validated_output_json = record.validated_output_json
        row.error_json = record.error_json
        return row

    def _append_agent_job_update_row(
        self,
        db: Any,
        job_id: str,
        *,
        status: str,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> Optional[AgentJobModel]:
        row = db.get(AgentJobModel, job_id)
        if not row:
            return None
        updated = AgentJobRecord.from_row(row).transition_to(
            status,
            started_at=started_at,
            completed_at=completed_at,
        )
        row.status = status
        row.started_at = updated.started_at
        row.completed_at = updated.completed_at
        return row

    def _agent_job_to_dict(self, row: AgentJobModel) -> JsonObject:
        compensations = self._execution_compensations_for_job(row.job_id) if row.job_type == "execution" else None
        return AgentJobRecord.from_row(row, compensations=compensations).to_payload()

    def _write_agent_job_input(self, job_id: str, job_type: str, payload: JsonObject) -> str:
        path = self.tmp_jobs_dir / job_id / job_type / "input.json"
        self._write_json(path, payload)
        return str(path)
