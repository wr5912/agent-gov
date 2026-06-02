from __future__ import annotations

from typing import Any, Optional

from .agent_profiles import (
    ATTRIBUTION_ANALYZER_PROFILE,
    EVAL_CASE_GOVERNOR_PROFILE,
    EXECUTION_OPTIMIZER_PROFILE,
    PROPOSAL_GENERATOR_PROFILE,
    REGRESSION_IMPACT_ANALYZER_PROFILE,
)
from .response_schemas.agent_job_response_schemas import AgentJobResponse
from .response_schemas.feedback_workflow_response_schemas import FeedbackOptimizationBatchResponse
from .schemas import EvalRunResponse


class FeedbackRuntimeJobsMixin:
    async def run_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> AgentJobResponse | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_attribution_job(feedback_case_id, force=force)

    def queue_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> AgentJobResponse | None:
        if self.feedback_store is None:
            return None
        return self._agent_job_response(
            self.feedback_store.queue_attribution_agent_job(
                feedback_case_id,
                profile_version=self.profile_version_snapshot(ATTRIBUTION_ANALYZER_PROFILE),
                force=force,
            )
        )

    async def run_proposal_job(
        self,
        feedback_case_id: str,
        *,
        force: bool = False,
        regeneration_instruction: Optional[str] = None,
    ) -> AgentJobResponse | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_proposal_job(
            feedback_case_id,
            force=force,
            regeneration_instruction=regeneration_instruction,
        )

    def queue_proposal_job(
        self,
        feedback_case_id: str,
        *,
        force: bool = False,
        regeneration_instruction: Optional[str] = None,
    ) -> AgentJobResponse | None:
        if self.feedback_store is None:
            return None
        return self._agent_job_response(
            self.feedback_store.queue_proposal_agent_job(
                feedback_case_id,
                profile_version=self.profile_version_snapshot(PROPOSAL_GENERATOR_PROFILE),
                force=force,
                regeneration_instruction=regeneration_instruction,
            )
        )

    async def run_batch_optimization_plan(
        self,
        batch_id: str,
        *,
        regeneration_instruction: Optional[str] = None,
        force: bool = True,
    ) -> FeedbackOptimizationBatchResponse | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_batch_optimization_plan(
            batch_id,
            regeneration_instruction=regeneration_instruction,
            force=force,
        )

    def queue_batch_optimization_plan(
        self,
        batch_id: str,
        *,
        regeneration_instruction: Optional[str] = None,
        force: bool = True,
    ) -> AgentJobResponse | None:
        if self.feedback_store is None:
            return None
        return self._agent_job_response(
            self.feedback_store.queue_batch_plan_agent_job(
                batch_id,
                profile_version=self.profile_version_snapshot(PROPOSAL_GENERATOR_PROFILE),
                force=force,
                regeneration_instruction=regeneration_instruction,
            )
        )

    async def run_execution_job(self, optimization_task_id: str, *, force: bool = False) -> AgentJobResponse | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_execution_job(optimization_task_id, force=force)

    def queue_execution_job(self, optimization_task_id: str, *, force: bool = False) -> AgentJobResponse | None:
        if self.feedback_store is None:
            return None
        return self._agent_job_response(
            self.feedback_store.queue_execution_agent_job(
                optimization_task_id,
                profile_version=self.profile_version_snapshot(EXECUTION_OPTIMIZER_PROFILE),
                force=force,
            )
        )

    def queue_eval_case_generation_job(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        source_refs: Optional[list[dict[str, Any]]] = None,
        batch_id: Optional[str] = None,
        limit: int = 100,
        force: bool = False,
    ) -> AgentJobResponse | None:
        if self.feedback_store is None:
            return None
        return self._agent_job_response(
            self.feedback_store.queue_feedback_eval_case_generation_agent_job(
                feedback_case_id=feedback_case_id,
                source_refs=source_refs,
                batch_id=batch_id,
                limit=limit,
                force=force,
                profile_version=self.profile_version_snapshot(EVAL_CASE_GOVERNOR_PROFILE),
            )
        )

    def queue_regression_impact_analysis_job(self, eval_run_id: str, *, force: bool = False) -> AgentJobResponse | None:
        if self.feedback_store is None:
            return None
        return self._agent_job_response(
            self.feedback_store.queue_regression_impact_agent_job(
                eval_run_id,
                profile_version=self.profile_version_snapshot(REGRESSION_IMPACT_ANALYZER_PROFILE),
                force=force,
            )
        )

    @staticmethod
    def _agent_job_response(payload: dict[str, Any] | None) -> AgentJobResponse | None:
        return AgentJobResponse.model_validate(payload) if payload else None

    async def run_feedback_eval(
        self,
        *,
        eval_case_ids: Optional[list[str]] = None,
        optimization_task_id: Optional[str] = None,
        source: str = "manual_feedback_dataset",
        regression_plan_id: Optional[str] = None,
        existing_eval_run_id: Optional[str] = None,
    ) -> EvalRunResponse | None:
        if self.eval_runner is None:
            return None
        return await self.eval_runner.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            optimization_task_id=optimization_task_id,
            source=source,
            regression_plan_id=regression_plan_id,
            existing_eval_run_id=existing_eval_run_id,
        )
