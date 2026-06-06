from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import cast

from app.runtime.agent_job_logging import log_agent_job_event
from app.runtime.agent_job_types import (
    AgentJobType,
    FormatterOutputModel,
    ProjectedOutputModel,
    agent_job_spec,
    coerce_agent_job_type,
)
from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.stores.feedback_store import FeedbackStore

RunProfileJson = Callable[..., Awaitable[FormatterOutputModel]]
logger = logging.getLogger(__name__)


def _agent_error_message(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


def _exception_raw_output_json(exc: Exception) -> JsonObject | None:
    raw_output = getattr(exc, "raw_output_json", None)
    return raw_output if isinstance(raw_output, dict) else None


class AgentJobWorker:
    """Runs queued generic Agent jobs and projects validated outputs."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        run_profile_json: RunProfileJson,
        poll_interval_seconds: float = 2.0,
        worker_instance: str | None = None,
    ) -> None:
        self.feedback_store = feedback_store
        self.run_profile_json = run_profile_json
        self.poll_interval_seconds = poll_interval_seconds
        self.worker_instance = worker_instance or f"{socket.gethostname()}:{os.getpid()}"

    async def run_once(self) -> AgentJobResponse | None:
        for timed_out in self.feedback_store._timeout_stale_agent_jobs():
            log_agent_job_event(logger, logging.WARNING, "agent_job.stale_timeout", timed_out, worker_instance=self.worker_instance)
        job = self.feedback_store.claim_next_agent_job()
        if not job:
            return None
        log_agent_job_event(logger, logging.INFO, "agent_job.claimed", job, worker_instance=self.worker_instance)
        try:
            job_output = await self._run_job(job)
            completed = self.feedback_store.complete_projected_agent_job(job, job_output)
            log_agent_job_event(logger, logging.INFO, "agent_job.completed", completed or job, worker_instance=self.worker_instance)
            return self._job_response(completed)
        except asyncio.TimeoutError as exc:
            failed = self.feedback_store.fail_projected_agent_job(
                job,
                error_code="AGENT_TIMEOUT",
                message=_agent_error_message(exc),
            )
            log_agent_job_event(logger, logging.WARNING, "agent_job.timeout", failed or job, worker_instance=self.worker_instance, error_code="AGENT_TIMEOUT")
            return self._job_response(failed)
        except Exception as exc:
            failed = self.feedback_store.fail_projected_agent_job(
                job,
                error_code="AGENT_RUNTIME_ERROR",
                message=_agent_error_message(exc),
                raw_output_json=_exception_raw_output_json(exc),
            )
            log_agent_job_event(
                logger,
                logging.ERROR,
                "agent_job.failed",
                failed or job,
                worker_instance=self.worker_instance,
                error_code="AGENT_RUNTIME_ERROR",
                exc_info=True,
            )
            return self._job_response(failed)

    async def run_forever(self) -> None:
        while True:
            result = await self.run_once()
            if result is None:
                await asyncio.sleep(self.poll_interval_seconds)

    async def _run_job(self, job: JsonObject) -> FormatterOutputModel | ProjectedOutputModel | JsonObject:
        job_type = coerce_agent_job_type(str(job.get("job_type") or ""))
        if job_type == AgentJobType.EXECUTION:
            execution = self.feedback_store.get_execution_job(str(job["job_id"]))
            deterministic = self.feedback_store.deterministic_execution_plan_output(execution or {})
            if deterministic:
                return deterministic
        job_input = cast(JsonObject, job.get("input_json")) if isinstance(job.get("input_json"), dict) else {}
        return await self.run_profile_json(
            profile_name=str(job["profile_name"]),
            prompt=self._prompt(job_type, job, job_input),
            job_type=job_type,
            job_input=job_input,
        )

    def _prompt(self, job_type: AgentJobType, job: JsonObject, job_input: JsonObject) -> str:
        input_path = str(job.get("input_path") or "")
        return agent_job_spec(job_type).prompt_builder(input_path, job_input)

    @staticmethod
    def _job_response(payload: JsonObject | None) -> AgentJobResponse | None:
        return AgentJobResponse.model_validate(payload) if payload else None
