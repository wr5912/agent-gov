from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select

from ..agent_job_types import agent_job_spec
from ..feedback_job_flags import has_no_actionable_attributions, reused_existing
from ..json_types import JsonObject
from ..records.regression_impact_records import RegressionImpactAnalysisRecord, apply_regression_impact_analysis_record
from ..runtime_db import FeedbackOptimizationBatchModel, RegressionImpactAnalysisModel, utc_now


AgentJobPayload = JsonObject


class AgentJobQueueStoreMixin:
    """Domain-specific factories for queued generic Agent jobs."""

    def queue_attribution_agent_job(
        self,
        feedback_case_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = False,
    ) -> Optional[AgentJobPayload]:
        domain_job = self.create_attribution_job(feedback_case_id, profile_version=profile_version, force=force)
        if not domain_job:
            return None
        return self._ensure_agent_job_for_domain_job(
            domain_job,
            scope_kind="feedback_case",
            scope_id=feedback_case_id,
            profile_version=profile_version,
        )

    def queue_feedback_case_optimization_plan_agent_job(
        self,
        feedback_case_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = True,
        regeneration_instruction: Optional[str] = None,
    ) -> Optional[AgentJobPayload]:
        batch = self.ensure_single_case_optimization_batch(feedback_case_id)
        if not batch:
            return None
        return self.queue_batch_plan_agent_job(
            str(batch["batch_id"]),
            profile_version=profile_version,
            force=force,
            regeneration_instruction=regeneration_instruction,
        )

    def queue_batch_plan_agent_job(
        self,
        batch_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = True,
        regeneration_instruction: Optional[str] = None,
    ) -> Optional[JsonObject]:
        domain_job = self.create_batch_plan_job(
            batch_id,
            profile_version=profile_version,
            force=force,
            regeneration_instruction=regeneration_instruction,
        )
        if not domain_job or has_no_actionable_attributions(domain_job):
            return None
        return self._ensure_agent_job_for_domain_job(
            domain_job,
            scope_kind="optimization_batch",
            scope_id=batch_id,
            profile_version=profile_version,
        )

    def queue_execution_agent_job(
        self,
        optimization_task_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = False,
    ) -> Optional[JsonObject]:
        domain_job = self.create_execution_job(optimization_task_id, profile_version=profile_version, force=force)
        if not domain_job:
            return None
        return self._ensure_agent_job_for_execution_job(domain_job, profile_version=profile_version)

    def queue_feedback_eval_case_generation_agent_job(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        source_refs: Optional[list[JsonObject]] = None,
        batch_id: Optional[str] = None,
        limit: int = 100,
        force: bool = False,
        profile_version: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        context = self._eval_case_generation_input_context(
            feedback_case_id=feedback_case_id,
            source_refs=source_refs,
            batch_id=batch_id,
            limit=limit,
            force=force,
        )
        if not context.get("feedback_cases") and not context.get("source_refs"):
            return None
        spec = agent_job_spec("eval_case_generation")
        job_id = f"evg-{uuid.uuid4()}"
        scope_kind = "optimization_batch" if batch_id else ("feedback_case" if feedback_case_id else "feedback_dataset")
        scope_id = batch_id or feedback_case_id or "feedback-dataset"
        input_payload = {
            "schema_version": "feedback-eval-case-generation-input/v1",
            "job_id": job_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "feedback_case_id": feedback_case_id,
            "batch_id": batch_id,
            "force": force,
            "task": "generate_feedback_eval_cases",
            **context,
        }
        job = self.create_agent_job(
            job_id=job_id,
            job_type=spec.job_type,
            scope_kind=scope_kind,
            scope_id=scope_id,
            profile_name=spec.profile_name,
            input_payload=input_payload,
            profile_version=profile_version,
        )
        if batch_id:
            self._attach_eval_case_generation_job_to_batch(batch_id, job)
        return job

    def queue_regression_impact_agent_job(
        self,
        eval_run_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = False,
    ) -> Optional[JsonObject]:
        eval_run = self.get_eval_run(eval_run_id)
        if not eval_run:
            return None
        existing = self.get_regression_impact_analysis(eval_run_id)
        if existing and existing.get("job_id") and not force:
            job = self.get_agent_job(str(existing["job_id"]))
            if job:
                return job
        spec = agent_job_spec("regression_impact_analysis")
        job_id = f"riaj-{uuid.uuid4()}"
        input_payload = {
            "schema_version": "regression-impact-analysis-input/v1",
            "job_id": job_id,
            "eval_run_id": eval_run_id,
            "eval_run": eval_run,
            "task": "analyze_regression_impact",
        }
        job = self.create_agent_job(
            job_id=job_id,
            job_type=spec.job_type,
            scope_kind="eval_run",
            scope_id=eval_run_id,
            profile_name=spec.profile_name,
            input_payload=input_payload,
            profile_version=profile_version,
        )
        self._upsert_pending_regression_impact(eval_run_id, job)
        return job

    def _ensure_agent_job_for_domain_job(
        self,
        domain_job: JsonObject,
        *,
        scope_kind: str,
        scope_id: str,
        profile_version: Optional[JsonObject],
    ) -> JsonObject:
        job_id = str(domain_job["job_id"])
        existing = self.get_agent_job(job_id)
        if existing:
            return existing
        spec = agent_job_spec(str(domain_job["job_type"]))
        status = str(domain_job.get("status") or "queued")
        return self.create_agent_job(
            job_id=job_id,
            job_type=spec.job_type,
            scope_kind=scope_kind,
            scope_id=scope_id,
            profile_name=str(domain_job.get("profile_name") or spec.profile_name),
            input_payload=domain_job.get("input_json") if isinstance(domain_job.get("input_json"), dict) else {},
            input_path=str(domain_job.get("input_path") or ""),
            profile_version=profile_version or domain_job.get("profile_version"),
            status=status if reused_existing(domain_job) else "queued",
        )

    def _ensure_agent_job_for_execution_job(
        self,
        domain_job: JsonObject,
        *,
        profile_version: Optional[JsonObject],
    ) -> JsonObject:
        job_id = str(domain_job["execution_job_id"])
        existing = self.get_agent_job(job_id)
        if existing:
            return existing
        spec = agent_job_spec("execution")
        domain_status = str(domain_job.get("status") or "queued")
        agent_status = "completed" if domain_status in {"ready", "completed"} else domain_status
        return self.create_agent_job(
            job_id=job_id,
            job_type=spec.job_type,
            scope_kind="optimization_task",
            scope_id=str(domain_job["optimization_task_id"]),
            profile_name=str(domain_job.get("profile_name") or spec.profile_name),
            input_payload=domain_job.get("input_json") if isinstance(domain_job.get("input_json"), dict) else {},
            input_path=str(domain_job.get("input_path") or ""),
            profile_version=profile_version or domain_job.get("profile_version"),
            status=agent_status if reused_existing(domain_job) else "queued",
        )

    def _eval_case_generation_input_context(
        self,
        *,
        feedback_case_id: Optional[str],
        source_refs: Optional[list[JsonObject]],
        batch_id: Optional[str],
        limit: int,
        force: bool,
    ) -> JsonObject:
        feedback_cases: list[JsonObject] = []
        prepared_source_refs: list[JsonObject] = []
        cases_to_create: list[JsonObject] = []
        if batch_id:
            batch = self.find_optimization_batch(batch_id)
            if not batch:
                return {"feedback_cases": [], "source_refs": []}
            source_refs = [ref for ref in batch.get("source_refs") or [] if isinstance(ref, dict)]
            for case_id in batch.get("feedback_case_ids") or []:
                case = self.find_case(str(case_id))
                if case:
                    feedback_cases.append(case)
        elif source_refs:
            for ref in self._normalize_source_refs(source_refs):
                feedback_case, should_create = self._prepare_feedback_case_for_source(ref, priority="medium")
                if not feedback_case:
                    continue
                prepared_source_refs.append(ref)
                feedback_cases.append(feedback_case)
                if should_create:
                    cases_to_create.append(feedback_case)
        else:
            feedback_cases = [case for case in ([self.find_case(feedback_case_id)] if feedback_case_id else self.list_cases(limit=limit)) if case]
        if cases_to_create:
            with self.Session.begin() as db:
                for feedback_case in cases_to_create:
                    db.add(self._case_model_from_dict(feedback_case))
        return {
            "force": force,
            "source_refs": prepared_source_refs or [ref for ref in source_refs or [] if isinstance(ref, dict)],
            "feedback_cases": [self._eval_case_generation_case_context(case) for case in feedback_cases],
            "existing_eval_cases": [
                case
                for case in (
                    self.find_eval_case(source_feedback_case_id=str(item.get("feedback_case_id") or ""))
                    for item in feedback_cases
                )
                if case
            ],
        }

    def _eval_case_generation_case_context(self, feedback_case: JsonObject) -> JsonObject:
        attribution_job_id = self._latest(feedback_case.get("attribution_job_ids"))
        optimization_plan = self._latest_optimization_plan_for_feedback_case(str(feedback_case.get("feedback_case_id") or ""))
        source_refs = [{"source_kind": "signal", "source_id": source_id} for source_id in feedback_case.get("signal_ids") or []]
        source_refs.extend({"source_kind": "soc_event", "source_id": source_id} for source_id in feedback_case.get("event_ids") or [])
        source_refs.extend(
            {"source_kind": "pending_correlation", "source_id": source_id}
            for source_id in feedback_case.get("pending_correlation_ids") or []
        )
        run_id = self._latest(feedback_case.get("run_ids"))
        return {
            "feedback_case": feedback_case,
            "source_refs": source_refs,
            "source_records": [self.find_feedback_source(ref["source_kind"], ref["source_id"]) for ref in source_refs],
            "source_run": self.find_run(run_id=run_id) if run_id else None,
            "attribution_output": self.get_job_output(str(attribution_job_id), "attribution") if attribution_job_id else None,
            "optimization_plan": optimization_plan,
        }

    def _latest_optimization_plan_for_feedback_case(self, feedback_case_id: str) -> Optional[JsonObject]:
        if not feedback_case_id:
            return None
        for batch in self.list_optimization_batches(limit=1000):
            if feedback_case_id not in set(batch.get("feedback_case_ids") or []):
                continue
            plan = batch.get("optimization_plan")
            if isinstance(plan, dict):
                return plan
        return None

    def _attach_eval_case_generation_job_to_batch(self, batch_id: str, job: JsonObject) -> None:
        with self.Session.begin() as db:
            row = db.get(FeedbackOptimizationBatchModel, batch_id)
            if not row:
                return
            self._update_batch_row(
                db,
                batch_id,
                status=row.status,
                fields={"eval_case_generation_job_id": job["job_id"], "eval_case_generation_job": job},
            )

    def _upsert_pending_regression_impact(self, eval_run_id: str, job: JsonObject) -> None:
        now = utc_now()
        with self.Session.begin() as db:
            row = db.scalars(select(RegressionImpactAnalysisModel).where(RegressionImpactAnalysisModel.eval_run_id == eval_run_id)).first()
            payload = {
                "schema_version": "regression-impact-analysis/v1",
                "impact_analysis_id": row.impact_analysis_id if row else f"ria-{uuid.uuid4()}",
                "eval_run_id": eval_run_id,
                "created_at": row.created_at if row else now,
                "completed_at": None,
                "status": "pending",
                "job_id": job["job_id"],
                "error_json": None,
            }
            record = (
                RegressionImpactAnalysisRecord.from_row(row).transition_to("pending", fields=payload)
                if row
                else RegressionImpactAnalysisRecord.model_validate(payload)
            )
            if row:
                apply_regression_impact_analysis_record(row, record)
                return
            db.add(
                RegressionImpactAnalysisModel(
                    impact_analysis_id=record.impact_analysis_id,
                    eval_run_id=record.eval_run_id,
                    created_at=record.created_at,
                    completed_at=None,
                    status=record.status,
                    job_id=record.job_id,
                    payload_json=record.to_payload(),
                )
            )
