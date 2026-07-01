from __future__ import annotations

from typing import Optional

from .agent_profiles import GOVERNOR_PROFILE
from .json_types import JsonObject
from .response_schemas.agent_job_response_schemas import AgentJobResponse
from .schemas import EvalRunResponse


class FeedbackRuntimeJobsMixin:
    def queue_eval_case_generation_job(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        source_refs: Optional[list[JsonObject]] = None,
        limit: int = 100,
        force: bool = False,
    ) -> AgentJobResponse | None:
        if self.feedback_store is None:
            return None
        return self._agent_job_response(
            self.feedback_store.queue_feedback_eval_case_generation_agent_job(
                feedback_case_id=feedback_case_id,
                source_refs=source_refs,
                limit=limit,
                force=force,
                profile_version=self.profile_version_snapshot(GOVERNOR_PROFILE),
            )
        )

    @staticmethod
    def _agent_job_response(payload: JsonObject | None) -> AgentJobResponse | None:
        return AgentJobResponse.model_validate(payload) if payload else None

    async def run_feedback_eval(
        self,
        *,
        eval_case_ids: Optional[list[str]] = None,
        source: str = "manual_feedback_dataset",
        existing_eval_run_id: Optional[str] = None,
        change_set_id: Optional[str] = None,
        candidate_commit_sha: Optional[str] = None,
        candidate_worktree_path: Optional[str] = None,
    ) -> EvalRunResponse | None:
        if self.eval_runner is None:
            return None
        return await self.eval_runner.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            source=source,
            existing_eval_run_id=existing_eval_run_id,
            change_set_id=change_set_id,
            candidate_commit_sha=candidate_commit_sha,
            candidate_worktree_path=candidate_worktree_path,
        )
