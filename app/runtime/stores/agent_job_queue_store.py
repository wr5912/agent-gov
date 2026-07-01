from __future__ import annotations

import uuid
from typing import Optional

from ..agent_job_types import agent_job_spec
from ..json_types import JsonObject


AgentJobPayload = JsonObject


class AgentJobQueueStoreMixin:
    """Domain-specific factories for queued generic Agent jobs still used by the product."""

    def queue_feedback_eval_case_generation_agent_job(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        source_refs: Optional[list[JsonObject]] = None,
        limit: int = 100,
        force: bool = False,
        profile_version: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        context = self._eval_case_generation_input_context(
            feedback_case_id=feedback_case_id,
            source_refs=source_refs,
            limit=limit,
            force=force,
        )
        if not context.get("feedback_cases") and not context.get("source_refs"):
            return None
        spec = agent_job_spec("eval_case_generation")
        job_id = f"evg-{uuid.uuid4()}"
        scope_kind = "feedback_case" if feedback_case_id else "feedback_dataset"
        scope_id = feedback_case_id or "feedback-dataset"
        input_payload = {
            "job_id": job_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "feedback_case_id": feedback_case_id,
            "force": force,
            "task": "generate_feedback_eval_cases",
            **context,
        }
        return self.create_agent_job(
            job_id=job_id,
            job_type=spec.job_type,
            scope_kind=scope_kind,
            scope_id=scope_id,
            profile_name=spec.profile_name,
            input_payload=input_payload,
            profile_version=profile_version,
        )

    def _eval_case_generation_input_context(
        self,
        *,
        feedback_case_id: Optional[str],
        source_refs: Optional[list[JsonObject]],
        limit: int,
        force: bool,
    ) -> JsonObject:
        feedback_cases: list[JsonObject] = []
        prepared_source_refs: list[JsonObject] = []
        cases_to_create: list[JsonObject] = []
        if source_refs:
            for ref in self._normalize_source_refs(source_refs):
                feedback_case, should_create = self._prepare_feedback_case_for_source(ref, priority="medium")
                if not feedback_case:
                    continue
                prepared_source_refs.append(ref)
                feedback_cases.append(feedback_case)
                if should_create:
                    cases_to_create.append(feedback_case)
        else:
            feedback_cases = [
                case
                for case in ([self.find_case(feedback_case_id)] if feedback_case_id else self.list_cases(limit=limit))
                if case
            ]
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
        }
