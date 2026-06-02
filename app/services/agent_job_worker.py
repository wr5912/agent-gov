from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, cast

from app.runtime.prompts.feedback_prompts import (
    attribution_prompt,
    batch_optimization_plan_prompt,
    eval_case_generation_prompt,
    execution_plan_prompt,
    proposal_prompt,
    regression_impact_analysis_prompt,
)
from app.runtime.records.json_types import JsonObject
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.stores.feedback_store import FeedbackStore


RunProfileJson = Callable[..., Awaitable[JsonObject]]


def _agent_error_message(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


class AgentJobWorker:
    """Runs queued generic Agent jobs and projects validated outputs."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        run_profile_json: RunProfileJson,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.feedback_store = feedback_store
        self.run_profile_json = run_profile_json
        self.poll_interval_seconds = poll_interval_seconds

    async def run_once(self) -> AgentJobResponse | None:
        job = self.feedback_store.claim_next_agent_job()
        if not job:
            return None
        try:
            raw = await self._run_job(job)
            return self._job_response(self.feedback_store.complete_projected_agent_job(job, raw))
        except asyncio.TimeoutError as exc:
            return self._job_response(
                self.feedback_store.fail_projected_agent_job(
                    job,
                    error_code="AGENT_TIMEOUT",
                    message=_agent_error_message(exc),
                )
            )
        except Exception as exc:
            return self._job_response(
                self.feedback_store.fail_projected_agent_job(
                    job,
                    error_code="AGENT_RUNTIME_ERROR",
                    message=_agent_error_message(exc),
                )
            )

    async def run_forever(self) -> None:
        while True:
            result = await self.run_once()
            if result is None:
                await asyncio.sleep(self.poll_interval_seconds)

    async def _run_job(self, job: JsonObject) -> JsonObject:
        if job.get("job_type") == "execution":
            execution = self.feedback_store.get_execution_job(str(job["job_id"]))
            deterministic = self.feedback_store.deterministic_execution_plan_output(execution or {})
            if deterministic:
                return deterministic
        job_input = cast(JsonObject, job.get("input_json")) if isinstance(job.get("input_json"), dict) else {}
        return await self.run_profile_json(
            profile_name=str(job["profile_name"]),
            prompt=self._prompt(job, job_input),
            expected_schema_version=str(job["output_schema_version"]),
            job_type=str(job["job_type"]),
            job_input=job_input,
        )

    def _prompt(self, job: JsonObject, job_input: JsonObject) -> str:
        job_type = str(job.get("job_type") or "")
        input_path = str(job.get("input_path") or "")
        if job_type == "attribution":
            return attribution_prompt(input_path)
        if job_type == "proposal":
            attribution_job_id = job_input.get("attribution_job_id")
            attribution_output = self.feedback_store.get_job_output(str(attribution_job_id), "attribution") if attribution_job_id else None
            return proposal_prompt(input_path, input_payload=job_input, attribution_output=attribution_output)
        if job_type == "batch_plan":
            return batch_optimization_plan_prompt(input_path, input_payload=job_input)
        if job_type == "execution":
            return execution_plan_prompt(input_path, input_payload=job_input)
        if job_type == "eval_case_generation":
            return eval_case_generation_prompt(input_path, input_payload=job_input)
        if job_type == "regression_impact_analysis":
            return regression_impact_analysis_prompt(input_path, input_payload=job_input)
        raise RuntimeError(f"Unsupported agent job type: {job_type}")

    @staticmethod
    def _job_response(payload: JsonObject | None) -> AgentJobResponse | None:
        return AgentJobResponse.model_validate(payload) if payload else None
