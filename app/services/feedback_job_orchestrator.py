from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Optional, cast

from app.runtime.agent_job_errors import agent_error_code, agent_error_message, exception_raw_output_json
from app.runtime.agent_job_types import AgentJobType, FormatterOutputModel, agent_job_spec
from app.runtime.agent_profile_versions import profile_version_snapshot
from app.runtime.agent_profiles import PROFILE_VERSION_IDS, AgentRuntimeProfile
from app.runtime.feedback_job_flags import has_no_actionable_attributions, reused_existing
from app.runtime.feedback_schemas import (
    AttributionFormatterOutput,
    ExecutionPlanFormatterOutput,
    FeedbackOptimizationPlanFormatterOutput,
)
from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.response_schemas.feedback_workflow_response_schemas import FeedbackOptimizationBatchResponse
from app.runtime.stores.feedback_store import FeedbackStore

RunProfileJson = Callable[..., Awaitable[FormatterOutputModel]]
JobResult = JsonObject | None


def _job_input(job: JsonObject) -> JsonObject:
    return cast(JsonObject, job.get("input_json")) if isinstance(job.get("input_json"), dict) else {}


def _agent_job_response(payload: JsonObject | None) -> AgentJobResponse | None:
    return AgentJobResponse.model_validate(payload) if payload else None


def _batch_response(payload: JsonObject | None) -> FeedbackOptimizationBatchResponse | None:
    return FeedbackOptimizationBatchResponse.model_validate(payload) if payload else None


class FeedbackJobOrchestrator:
    """Coordinates feedback-loop Agent jobs while FeedbackStore owns persistence."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        profiles: dict[str, AgentRuntimeProfile],
        run_profile_json: RunProfileJson,
    ) -> None:
        self.feedback_store = feedback_store
        self.profiles = profiles
        self.run_profile_json = run_profile_json

    async def run_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> AgentJobResponse | None:
        spec = agent_job_spec(AgentJobType.ATTRIBUTION)
        profile = self.profiles[spec.profile_name]
        job = self.feedback_store.create_attribution_job(
            feedback_case_id,
            profile_version=profile_version_snapshot(profile, version_id=PROFILE_VERSION_IDS[spec.profile_name]),
            force=force,
        )
        if not job:
            return None
        if reused_existing(job) or job.get("status") != "queued":
            return _agent_job_response(job)
        self.feedback_store.start_job(job["job_id"])
        return _agent_job_response(
            await self._run_profile_json_job(
                profile_name=spec.profile_name,
                prompt=spec.prompt_builder(_job_input(job)),
                job_type=spec.job_type,
                job_input=_job_input(job),
                complete=lambda formatter_output: self.feedback_store.complete_attribution_job(
                    job["job_id"],
                    cast(AttributionFormatterOutput, formatter_output),
                ),
                fail=lambda code, message, raw_output=None: self.feedback_store.fail_job(
                    job["job_id"], error_code=code, message=message, raw_output_json=raw_output
                ),
                final_result=lambda: self.feedback_store.get_job(job["job_id"]),
            )
        )

    async def run_batch_optimization_plan(
        self,
        batch_id: str,
        *,
        regeneration_instruction: Optional[str] = None,
        force: bool = True,
    ) -> FeedbackOptimizationBatchResponse | None:
        spec = agent_job_spec(AgentJobType.BATCH_PLAN)
        profile = self.profiles[spec.profile_name]
        job = self.feedback_store.create_batch_plan_job(
            batch_id,
            profile_version=profile_version_snapshot(profile, version_id=PROFILE_VERSION_IDS[spec.profile_name]),
            force=force,
            regeneration_instruction=regeneration_instruction,
        )
        if not job:
            return _batch_response(self.feedback_store.find_optimization_batch(batch_id))
        if has_no_actionable_attributions(job) or reused_existing(job) or job.get("status") != "queued":
            return _batch_response(self.feedback_store.find_optimization_batch(batch_id))
        self.feedback_store.start_job(job["job_id"])
        input_payload = _job_input(job)
        return _batch_response(
            await self._run_profile_json_job(
                profile_name=spec.profile_name,
                prompt=spec.prompt_builder(input_payload),
                job_type=spec.job_type,
                job_input=input_payload,
                complete=lambda formatter_output: self.feedback_store.complete_batch_plan_job(
                    job["job_id"],
                    cast(FeedbackOptimizationPlanFormatterOutput, formatter_output),
                ),
                fail=lambda code, message, raw_output=None: self.feedback_store.fail_job(
                    job["job_id"], error_code=code, message=message, raw_output_json=raw_output
                ),
                final_result=lambda: self.feedback_store.find_optimization_batch(batch_id),
            )
        )

    async def run_execution_job(self, optimization_task_id: str, *, force: bool = False) -> AgentJobResponse | None:
        spec = agent_job_spec(AgentJobType.EXECUTION)
        profile = self.profiles[spec.profile_name]
        job = self.feedback_store.create_execution_job(
            optimization_task_id,
            profile_version=profile_version_snapshot(profile, version_id=PROFILE_VERSION_IDS[spec.profile_name]),
            force=force,
        )
        if not job:
            return None
        if reused_existing(job) or job.get("status") != "queued":
            return _agent_job_response(job)
        self.feedback_store.start_execution_job(job["execution_job_id"])
        deterministic_plan = self.feedback_store.deterministic_execution_plan_output(job)
        if deterministic_plan:
            self.feedback_store.complete_execution_job(job["execution_job_id"], deterministic_plan)
            return _agent_job_response(self.feedback_store.get_execution_job(job["execution_job_id"]))
        input_payload = _job_input(job)
        return _agent_job_response(
            await self._run_profile_json_job(
                profile_name=spec.profile_name,
                prompt=spec.prompt_builder(input_payload),
                job_type=spec.job_type,
                job_input=input_payload,
                complete=lambda formatter_output: self.feedback_store.complete_execution_job(
                    job["execution_job_id"],
                    cast(ExecutionPlanFormatterOutput, formatter_output),
                ),
                fail=lambda code, message, raw_output=None: self.feedback_store.fail_execution_job(
                    job["execution_job_id"], error_code=code, message=message, raw_output_json=raw_output
                ),
                final_result=lambda: self.feedback_store.get_execution_job(job["execution_job_id"]),
            )
        )

    async def _run_profile_json_job(
        self,
        *,
        profile_name: str,
        prompt: str,
        job_type: AgentJobType,
        job_input: JsonObject,
        complete: Callable[[FormatterOutputModel], object],
        fail: Callable[[str, str, JsonObject | None], object],
        final_result: Callable[[], JobResult],
    ) -> JobResult:
        try:
            formatter_output = await self.run_profile_json(
                profile_name=profile_name,
                prompt=prompt,
                job_type=job_type,
                job_input=job_input,
            )
            complete(formatter_output)
        except asyncio.TimeoutError as exc:
            fail("AGENT_TIMEOUT", agent_error_message(exc), None)
        except Exception as exc:
            fail(agent_error_code(exc), agent_error_message(exc), exception_raw_output_json(exc))
        return final_result()
