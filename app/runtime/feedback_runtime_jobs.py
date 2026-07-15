from __future__ import annotations

from typing import Optional

from .schemas import EvalRunResponse


class FeedbackRuntimeJobsMixin:
    async def run_feedback_eval(
        self,
        *,
        dataset_id: str,
        source: str = "manual_feedback_dataset",
        change_set_id: Optional[str] = None,
        regression_attempt_id: Optional[str] = None,
        candidate_commit_sha: Optional[str] = None,
        candidate_worktree_path: Optional[str] = None,
    ) -> EvalRunResponse | None:
        if self.eval_runner is None:
            return None
        return await self.eval_runner.run_feedback_eval(
            dataset_id=dataset_id,
            source=source,
            change_set_id=change_set_id,
            regression_attempt_id=regression_attempt_id,
            candidate_commit_sha=candidate_commit_sha,
            candidate_worktree_path=candidate_worktree_path,
        )
