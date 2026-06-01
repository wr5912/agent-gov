from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select, update

from ..feedback_schemas import (
    validate_feedback_eval_case_generation_output,
    validate_regression_impact_analysis_output,
)
from ..runtime_db import (
    AgentJobModel,
    FeedbackOptimizationBatchModel,
    RegressionImpactAnalysisModel,
    utc_now,
)
from ..schema_versions import FEEDBACK_EVAL_CASE_SCHEMA_VERSION
from ..state_machines import validate_transition


_UNSET = object()


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
        input_payload: dict[str, Any],
        output_schema_version: str,
        input_path: Optional[str] = None,
        profile_version: Optional[dict[str, Any]] = None,
        status: str = "queued",
    ) -> dict[str, Any]:
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
            output_schema_version=output_schema_version,
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
        return self.get_agent_job(job_id) or self._agent_job_to_dict(row)

    def get_agent_job(self, job_id: str) -> Optional[dict[str, Any]]:
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
    ) -> list[dict[str, Any]]:
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

    def claim_next_agent_job(self, *, job_types: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
        now = utc_now()
        with self.Session.begin() as db:
            stmt = select(AgentJobModel).where(AgentJobModel.status == "queued").order_by(AgentJobModel.created_at.asc()).limit(20)
            if job_types:
                stmt = stmt.where(AgentJobModel.job_type.in_(job_types))
            for candidate in db.scalars(stmt).all():
                validate_transition("agent_job", candidate.status, "running")
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

    def complete_projected_agent_job(self, job: dict[str, Any], raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        job_type = str(job.get("job_type") or "")
        job_id = str(job.get("job_id") or "")
        if job_type == "attribution":
            projected = self.complete_attribution_job(job_id, raw_output)
            self._sync_attribution_agent_job_to_batches(job, projected)
            return projected
        if job_type == "proposal":
            return self.complete_proposal_job(job_id, raw_output)
        if job_type == "batch_plan":
            return self.complete_batch_plan_job(job_id, raw_output)
        if job_type == "execution":
            return self.complete_execution_job(job_id, raw_output)
        if job_type == "eval_case_generation":
            return self._complete_eval_case_generation_agent_job(job, raw_output)
        if job_type == "regression_impact_analysis":
            return self._complete_regression_impact_agent_job(job, raw_output)
        return self.fail_agent_job(job_id, error_code="UNSUPPORTED_AGENT_JOB_TYPE", message=f"Unsupported agent job type: {job_type}")

    def fail_projected_agent_job(self, job: dict[str, Any], *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        job_type = str(job.get("job_type") or "")
        job_id = str(job.get("job_id") or "")
        if job_type in {"attribution", "proposal", "batch_plan"}:
            return self.fail_job(job_id, error_code=error_code, message=message)
        elif job_type == "execution":
            return self.fail_execution_job(job_id, error_code=error_code, message=message)
        elif job_type == "regression_impact_analysis":
            self._fail_regression_impact_projection(job, error_code=error_code, message=message)
        return self.fail_agent_job(job_id, error_code=error_code, message=message)

    def fail_agent_job(self, job_id: str, *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        error_payload = {"error_code": error_code, "message": message, "created_at": utc_now(), "job_id": job_id}
        with self.Session.begin() as db:
            row = self._set_agent_job_json_row(db, job_id, error_json=error_payload)
            if not row:
                return None
            self._append_agent_job_update_row(db, job_id, status="failed", completed_at=utc_now())
        return self.get_agent_job(job_id)

    def _complete_agent_job_from_domain(
        self,
        job_id: str,
        projected: Optional[dict[str, Any]],
        *,
        ready_status: str = "completed",
    ) -> Optional[dict[str, Any]]:
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
    ) -> Optional[dict[str, Any]]:
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

    def _complete_eval_case_generation_agent_job(self, job: dict[str, Any], raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        validated, error = validate_feedback_eval_case_generation_output(raw_output)
        if not validated:
            return self._complete_agent_job(
                str(job["job_id"]),
                raw_output_json=raw_output,
                error_json={"error_code": "SCHEMA_VALIDATION_FAILED", "message": error or "invalid eval case generation output"},
                status="needs_human_review",
            )
        projected = self._project_eval_case_generation(job, validated)
        return self._complete_agent_job(
            str(job["job_id"]),
            raw_output_json=raw_output,
            validated_output_json=projected,
            error_json=None,
            status="completed" if projected.get("status") == "completed" else "needs_human_review",
        )

    def _complete_regression_impact_agent_job(self, job: dict[str, Any], raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        output = dict(raw_output)
        output["eval_run_id"] = output.get("eval_run_id") or job.get("scope_id")
        validated, error = validate_regression_impact_analysis_output(output)
        if not validated:
            self._fail_regression_impact_projection(job, error_code="SCHEMA_VALIDATION_FAILED", message=error or "invalid impact output")
            return self._complete_agent_job(
                str(job["job_id"]),
                raw_output_json=raw_output,
                error_json={"error_code": "SCHEMA_VALIDATION_FAILED", "message": error or "invalid impact output"},
                status="needs_human_review",
            )
        projected = self._project_regression_impact(job, validated)
        return self._complete_agent_job(
            str(job["job_id"]),
            raw_output_json=raw_output,
            validated_output_json=projected,
            error_json=None,
            status="completed" if projected.get("status") == "completed" else "needs_human_review",
        )

    def _sync_attribution_agent_job_to_batches(self, job: dict[str, Any], projected: Optional[dict[str, Any]]) -> None:
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

    def _project_eval_case_generation(self, job: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        job_input = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        force = bool(job_input.get("force"))
        created = reused = updated = skipped = 0
        eval_cases: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        now = utc_now()
        with self.Session.begin() as db:
            for item in output.get("eval_cases") or []:
                if not isinstance(item, dict) or not self._string(item.get("prompt")):
                    skipped += 1
                    results.append({"status": "skipped", "reason": "missing prompt"})
                    continue
                payload = self._eval_case_payload_from_agent(item, job_input, now)
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
            "status": "completed" if eval_cases else "needs_human_review",
            "created": created,
            "reused": reused,
            "updated": updated,
            "skipped": skipped,
            "eval_cases": eval_cases,
            "results": results,
        }

    def _project_regression_impact(self, job: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        eval_run_id = str(output.get("eval_run_id") or job.get("scope_id") or "")
        created_at = self._string(output.get("created_at")) or utc_now()
        completed_at = utc_now()
        with self.Session.begin() as db:
            row = db.scalars(select(RegressionImpactAnalysisModel).where(RegressionImpactAnalysisModel.eval_run_id == eval_run_id)).first()
            impact_analysis_id = self._string(output.get("impact_analysis_id")) or (row.impact_analysis_id if row else f"ria-{uuid.uuid4()}")
            payload = {
                **output,
                "impact_analysis_id": impact_analysis_id,
                "eval_run_id": eval_run_id,
                "created_at": row.created_at if row else created_at,
                "completed_at": completed_at,
                "status": output.get("status") or "completed",
                "job_id": job["job_id"],
            }
            if row:
                row.completed_at = completed_at
                row.status = str(payload["status"])
                row.job_id = str(job["job_id"])
                row.payload_json = payload
            else:
                db.add(
                    RegressionImpactAnalysisModel(
                        impact_analysis_id=impact_analysis_id,
                        eval_run_id=eval_run_id,
                        created_at=str(payload["created_at"]),
                        completed_at=completed_at,
                        status=str(payload["status"]),
                        job_id=str(job["job_id"]),
                        payload_json=payload,
                    )
                )
        return self.get_regression_impact_analysis(eval_run_id) or payload

    def _fail_regression_impact_projection(self, job: dict[str, Any], *, error_code: str, message: str) -> None:
        eval_run_id = str(job.get("scope_id") or "")
        with self.Session.begin() as db:
            row = db.scalars(select(RegressionImpactAnalysisModel).where(RegressionImpactAnalysisModel.eval_run_id == eval_run_id)).first()
            if not row:
                return
            payload = dict(row.payload_json or {})
            payload.update(
                {
                    "status": "failed",
                    "completed_at": utc_now(),
                    "job_id": job.get("job_id"),
                    "error_json": {"error_code": error_code, "message": message, "created_at": utc_now()},
                }
            )
            row.status = "failed"
            row.completed_at = payload["completed_at"]
            row.payload_json = payload

    def _eval_case_payload_from_agent(self, item: dict[str, Any], job_input: dict[str, Any], now: str) -> dict[str, Any]:
        payload = dict(item)
        payload["schema_version"] = payload.get("schema_version") or FEEDBACK_EVAL_CASE_SCHEMA_VERSION
        payload["eval_case_id"] = self._string(payload.get("eval_case_id")) or f"evc-{uuid.uuid4()}"
        payload["created_at"] = self._string(payload.get("created_at")) or now
        payload["updated_at"] = now
        payload["status"] = self._string(payload.get("status")) or "draft"
        payload["source"] = self._string(payload.get("source")) or "eval_case_governor"
        payload["source_feedback_case_id"] = self._string(payload.get("source_feedback_case_id")) or self._string(job_input.get("feedback_case_id"))
        payload["source_run_id"] = self._string(payload.get("source_run_id"))
        payload["prompt"] = str(payload.get("prompt") or "").strip()
        payload["expected_behavior"] = self._string(payload.get("expected_behavior")) or ""
        payload["checks_json"] = payload.get("checks_json") if isinstance(payload.get("checks_json"), dict) else {}
        payload["labels"] = self._unique_strings([*(payload.get("labels") or []), "feedback_optimization"])
        return self._eval_case_with_asset_defaults(payload)

    def _eval_case_generation_result(self, payload: dict[str, Any], eval_case: dict[str, Any], status: str) -> dict[str, Any]:
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
        job: dict[str, Any],
        eval_cases: list[dict[str, Any]],
        created: int,
        reused: int,
        updated: int,
        skipped: int,
        results: list[dict[str, Any]],
    ) -> None:
        if job.get("scope_kind") != "optimization_batch":
            return
        batch_id = str(job.get("scope_id") or "")
        row = db.get(FeedbackOptimizationBatchModel, batch_id)
        if not row:
            return
        payload = dict(row.payload_json or {})
        eval_case_ids = self._unique_strings([*(payload.get("eval_case_ids") or []), *[case.get("eval_case_id") for case in eval_cases]])
        payload.update(
            {
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
            }
        )
        row.payload_json = payload

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
        if raw_output_json is not _UNSET:
            row.raw_output_json = raw_output_json
        if validated_output_json is not _UNSET:
            row.validated_output_json = validated_output_json
        if error_json is not _UNSET:
            row.error_json = error_json
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
        validate_transition("agent_job", row.status, status)
        row.status = status
        if started_at is not None:
            row.started_at = started_at
        if completed_at is not None:
            row.completed_at = completed_at
        return row

    def _agent_job_to_dict(self, row: AgentJobModel) -> dict[str, Any]:
        payload = {
            "job_id": row.job_id,
            "job_type": row.job_type,
            "scope_kind": row.scope_kind,
            "scope_id": row.scope_id,
            "status": row.status,
            "profile_name": row.profile_name,
            "created_at": row.created_at,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "input_path": row.input_path,
            "raw_output_path": row.raw_output_path,
            "validated_output_path": row.validated_output_path,
            "error_path": row.error_path,
            "runtime_version": row.runtime_version,
            "schema_version": row.schema_version,
            "output_schema_version": row.output_schema_version,
            "timeout_seconds": row.timeout_seconds,
            "retry_count": row.retry_count,
            "profile_version": row.profile_version_json,
            "input_json": row.input_json,
            "raw_output_json": row.raw_output_json,
            "validated_output_json": row.validated_output_json,
            "error_json": row.error_json,
        }
        input_json = row.input_json if isinstance(row.input_json, dict) else {}
        for key in (
            "feedback_case_id",
            "evidence_package_id",
            "attribution_job_id",
            "batch_id",
            "optimization_task_id",
            "execution_job_id",
            "baseline_agent_version_id",
            "eval_run_id",
            "regression_plan_id",
        ):
            if input_json.get(key) is not None:
                payload[key] = input_json.get(key)
        if row.job_type == "execution":
            payload["execution_job_id"] = payload.get("execution_job_id") or row.job_id
            payload["compensations"] = self._execution_compensations_for_job(row.job_id)
        return payload

    def _write_agent_job_input(self, job_id: str, job_type: str, payload: dict[str, Any]) -> str:
        path = self.tmp_jobs_dir / job_id / job_type / "input.json"
        self._write_json(path, payload)
        return str(path)
