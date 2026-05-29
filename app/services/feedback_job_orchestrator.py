from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from app.runtime.agent_profiles import (
    ATTRIBUTION_ANALYZER_PROFILE,
    EXECUTION_OPTIMIZER_PROFILE,
    PROFILE_VERSION_IDS,
    PROPOSAL_GENERATOR_PROFILE,
    AgentRuntimeProfile,
)
from app.runtime.agent_profile_versions import profile_version_snapshot
from app.runtime.prompts.feedback_prompts import (
    attribution_prompt,
    batch_optimization_plan_prompt,
    execution_plan_prompt,
    proposal_prompt,
)
from app.runtime.feedback_job_flags import has_no_actionable_attributions, reused_existing
from app.runtime.stores.feedback_store import FeedbackStore

RunProfileJson = Callable[..., Awaitable[dict[str, Any]]]
JobResult = dict[str, Any] | None


def _job_input(job: dict[str, Any]) -> dict[str, Any]:
    return job.get("input_json") if isinstance(job.get("input_json"), dict) else {}


def _agent_error_message(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


class FeedbackJobOrchestrator:
    """Coordinates feedback-loop Agent jobs while FeedbackStore owns persistence."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        profiles: dict[str, AgentRuntimeProfile],
        provider_configured: Callable[[], bool],
        run_profile_json: RunProfileJson,
    ) -> None:
        self.feedback_store = feedback_store
        self.profiles = profiles
        self.provider_configured = provider_configured
        self.run_profile_json = run_profile_json

    async def run_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> dict[str, Any] | None:
        profile = self.profiles[ATTRIBUTION_ANALYZER_PROFILE]
        job = self.feedback_store.create_attribution_job(
            feedback_case_id,
            profile_version=profile_version_snapshot(profile, version_id=PROFILE_VERSION_IDS[ATTRIBUTION_ANALYZER_PROFILE]),
            force=force,
        )
        if not job:
            return None
        if reused_existing(job) or job.get("status") != "queued":
            return job
        self.feedback_store.start_job(job["job_id"])
        return await self._run_profile_json_job(
            profile_name=ATTRIBUTION_ANALYZER_PROFILE,
            prompt=attribution_prompt(job["input_path"]),
            expected_schema_version="attribution-output/v1",
            job_type="attribution",
            job_input=_job_input(job),
            complete=lambda raw: self.feedback_store.complete_attribution_job(job["job_id"], raw),
            fail=lambda code, message: self.feedback_store.fail_job(job["job_id"], error_code=code, message=message),
            final_result=lambda: self.feedback_store.get_job(job["job_id"]),
            offline_output=self.feedback_store.offline_attribution_output(job),
        )

    async def run_proposal_job(
        self,
        feedback_case_id: str,
        *,
        force: bool = False,
        regeneration_instruction: Optional[str] = None,
    ) -> dict[str, Any] | None:
        profile = self.profiles[PROPOSAL_GENERATOR_PROFILE]
        job = self.feedback_store.create_proposal_job(
            feedback_case_id,
            profile_version=profile_version_snapshot(profile, version_id=PROFILE_VERSION_IDS[PROPOSAL_GENERATOR_PROFILE]),
            force=force,
            regeneration_instruction=regeneration_instruction,
        )
        if not job:
            return None
        if reused_existing(job) or job.get("status") != "queued":
            return job
        self.feedback_store.start_job(job["job_id"])
        attribution_job_id = job.get("attribution_job_id")
        attribution_output = self.feedback_store.get_job_output(str(attribution_job_id), "attribution") if attribution_job_id else None
        return await self._run_profile_json_job(
            profile_name=PROPOSAL_GENERATOR_PROFILE,
            prompt=proposal_prompt(
                job["input_path"],
                input_payload=job.get("input_json"),
                attribution_output=attribution_output,
            ),
            expected_schema_version="proposal-output/v1",
            job_type="proposal",
            job_input=_job_input(job),
            complete=lambda raw: self.feedback_store.complete_proposal_job(job["job_id"], raw),
            fail=lambda code, message: self.feedback_store.fail_job(job["job_id"], error_code=code, message=message),
            final_result=lambda: self.feedback_store.get_job(job["job_id"]),
            offline_output=self.feedback_store.offline_proposal_output(job),
        )

    async def run_batch_optimization_plan(
        self,
        batch_id: str,
        *,
        regeneration_instruction: Optional[str] = None,
        force: bool = True,
    ) -> dict[str, Any] | None:
        profile = self.profiles[PROPOSAL_GENERATOR_PROFILE]
        job = self.feedback_store.create_batch_plan_job(
            batch_id,
            profile_version=profile_version_snapshot(profile, version_id=PROFILE_VERSION_IDS[PROPOSAL_GENERATOR_PROFILE]),
            force=force,
            regeneration_instruction=regeneration_instruction,
        )
        if not job:
            return self.feedback_store.find_optimization_batch(batch_id)
        if has_no_actionable_attributions(job) or reused_existing(job) or job.get("status") != "queued":
            return self.feedback_store.find_optimization_batch(batch_id)
        self.feedback_store.start_job(job["job_id"])
        input_payload = _job_input(job)
        return await self._run_profile_json_job(
            profile_name=PROPOSAL_GENERATOR_PROFILE,
            prompt=batch_optimization_plan_prompt(job["input_path"], input_payload=input_payload),
            expected_schema_version="feedback-optimization-plan-output/v1",
            job_type="batch_plan",
            job_input=input_payload,
            complete=lambda raw: self.feedback_store.complete_batch_plan_job(job["job_id"], raw),
            fail=lambda code, message: self.feedback_store.fail_job(job["job_id"], error_code=code, message=message),
            final_result=lambda: self.feedback_store.find_optimization_batch(batch_id),
            offline_output=self.feedback_store.offline_batch_plan_output(job),
        )

    async def run_execution_job(self, optimization_task_id: str, *, force: bool = False) -> dict[str, Any] | None:
        profile = self.profiles[EXECUTION_OPTIMIZER_PROFILE]
        job = self.feedback_store.create_execution_job(
            optimization_task_id,
            profile_version=profile_version_snapshot(profile, version_id=PROFILE_VERSION_IDS[EXECUTION_OPTIMIZER_PROFILE]),
            force=force,
        )
        if not job:
            return None
        if reused_existing(job) or job.get("status") != "queued":
            return job
        self.feedback_store.start_execution_job(job["execution_job_id"])
        deterministic_plan = self.feedback_store.deterministic_execution_plan_output(job)
        if deterministic_plan:
            self.feedback_store.complete_execution_job(job["execution_job_id"], deterministic_plan)
            return self.feedback_store.get_execution_job(job["execution_job_id"])
        if not self.provider_configured():
            self.feedback_store.complete_execution_job(job["execution_job_id"], self.feedback_store.offline_execution_plan_output(job))
            return self.feedback_store.get_execution_job(job["execution_job_id"])
        input_path = job.get("input_path")
        if not isinstance(input_path, str) or not input_path:
            input_path = str(self.feedback_store.tmp_jobs_dir / job["execution_job_id"] / "execution" / "input.json")
        input_payload = _job_input(job)
        return await self._run_profile_json_job(
            profile_name=EXECUTION_OPTIMIZER_PROFILE,
            prompt=execution_plan_prompt(input_path, input_payload=input_payload),
            expected_schema_version="execution-plan-output/v1",
            job_type="execution",
            job_input=input_payload,
            complete=lambda raw: self.feedback_store.complete_execution_job(job["execution_job_id"], raw),
            fail=lambda code, message: self.feedback_store.fail_execution_job(job["execution_job_id"], error_code=code, message=message),
            final_result=lambda: self.feedback_store.get_execution_job(job["execution_job_id"]),
        )

    async def _run_profile_json_job(
        self,
        *,
        profile_name: str,
        prompt: str,
        expected_schema_version: str,
        job_type: str,
        job_input: dict[str, Any],
        complete: Callable[[dict[str, Any]], Any],
        fail: Callable[[str, str], Any],
        final_result: Callable[[], JobResult],
        offline_output: dict[str, Any] | None = None,
    ) -> JobResult:
        if offline_output is not None and not self.provider_configured():
            complete(offline_output)
            return final_result()
        try:
            raw = await self.run_profile_json(
                profile_name=profile_name,
                prompt=prompt,
                expected_schema_version=expected_schema_version,
                job_type=job_type,
                job_input=job_input,
            )
            complete(raw)
        except asyncio.TimeoutError as exc:
            fail("AGENT_TIMEOUT", _agent_error_message(exc))
        except Exception as exc:
            fail("AGENT_RUNTIME_ERROR", _agent_error_message(exc))
        return final_result()
