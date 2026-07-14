from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import cast

from app.runtime.agent_job_errors import agent_error_code, agent_error_message, exception_raw_output_json
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


class AgentJobWorker:
    """Runs queued generic Agent jobs and projects validated outputs."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        run_profile_json: RunProfileJson,
        poll_interval_seconds: float = 2.0,
        worker_instance: str | None = None,
        can_claim_jobs: Callable[[], bool] | None = None,
    ) -> None:
        self.feedback_store = feedback_store
        self.run_profile_json = run_profile_json
        self.poll_interval_seconds = poll_interval_seconds
        self.worker_instance = worker_instance or f"{socket.gethostname()}:{os.getpid()}"
        self.can_claim_jobs = can_claim_jobs

    async def run_once(self) -> AgentJobResponse | None:
        if self.can_claim_jobs is not None and not self.can_claim_jobs():
            return None
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
                message=agent_error_message(exc),
            )
            log_agent_job_event(logger, logging.WARNING, "agent_job.timeout", failed or job, worker_instance=self.worker_instance, error_code="AGENT_TIMEOUT")
            return self._job_response(failed)
        except Exception as exc:
            error_code = agent_error_code(exc)
            failed = self.feedback_store.fail_projected_agent_job(
                job,
                error_code=error_code,
                message=agent_error_message(exc),
                raw_output_json=exception_raw_output_json(exc),
            )
            log_agent_job_event(
                logger,
                logging.ERROR,
                "agent_job.failed",
                failed or job,
                worker_instance=self.worker_instance,
                error_code=error_code,
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
        job_input = cast(JsonObject, job.get("input_json")) if isinstance(job.get("input_json"), dict) else {}
        # 治理 job 富化上下文：使其 Langfuse trace 走 runtime.governor.{job_type} + scope sessionId
        # + role:governance tag，与业务 Agent 区分（整改方案 §4.4 / §5.6）。
        governor = {
            "job_type": str(job_type),
            "scope_kind": str(job.get("scope_kind") or ""),
            "scope_id": str(job.get("scope_id") or ""),
            "job_id": str(job.get("job_id") or ""),
        }
        return await self.run_profile_json(
            # 执行期 profile 由 job_type→spec 解析（合并后恒为 governor）；持久化的
            # job["profile_name"] 仅为历史元数据/展示，不参与执行。合并前 queued 的旧 job
            # 其 profile_name 可能是 attribution-analyzer 等已删除的名字，按持久化值查
            # profiles 字典会 KeyError，故这里以 spec.profile_name 为唯一权威来源。
            profile_name=agent_job_spec(job_type).profile_name,
            prompt=self._prompt(job_type, job, job_input),
            job_type=job_type,
            job_input=job_input,
            governor=governor,
        )

    def _prompt(self, job_type: AgentJobType, job: JsonObject, job_input: JsonObject) -> str:
        return agent_job_spec(job_type).prompt_builder(job_input)

    @staticmethod
    def _job_response(payload: JsonObject | None) -> AgentJobResponse | None:
        return AgentJobResponse.model_validate(payload) if payload else None
