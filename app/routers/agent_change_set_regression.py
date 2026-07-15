from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.routers.error_helpers import ensure_found, raise_conflict
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetRegressionReviewRequest,
    AgentChangeSetRegressionRunRequest,
)
from app.runtime.schemas import EvalRunResponse
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService


def create_agent_change_set_regression_router(
    *,
    agent_governance: AgentGovernanceService,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/agent-change-sets/{change_set_id}/regression-runs",
        response_model=EvalRunResponse,
        summary="Run regression against an Agent change set candidate worktree",
    )
    async def run_agent_change_set_regression(
        change_set_id: str,
        req: AgentChangeSetRegressionRunRequest,
    ) -> EvalRunResponse:
        change_set = ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")
        candidate = _require_candidate_commit(change_set)
        agent_governance.validate_regression_dataset(
            change_set_id=change_set_id,
            dataset_id=req.dataset_id,
            candidate_commit_sha=candidate,
        )
        intent_id = f"evr-intent-{uuid.uuid4()}"
        agent_governance.mark_regression_running(
            change_set_id,
            eval_run_id=intent_id,
            dataset_id=req.dataset_id,
        )
        try:
            result = await runtime.run_feedback_eval(
                dataset_id=req.dataset_id,
                source="agent_change_set_regression",
                change_set_id=change_set_id,
                regression_attempt_id=intent_id,
                candidate_commit_sha=candidate,
                candidate_worktree_path=str(change_set["worktree_path"]),
            )
            if not result:
                raise_conflict("Regression run could not be started")
            agent_governance.complete_regression(
                change_set_id,
                eval_run_id=result.eval_run_id,
            )
        except BaseException as exc:
            try:
                agent_governance.fail_regression(
                    change_set_id,
                    expected_eval_run_id=intent_id,
                    error_type=type(exc).__name__,
                )
            except AgentGovernanceError as failure_error:
                if failure_error.status_code != 409:
                    raise
            raise
        return result

    @router.post(
        "/agent-change-sets/{change_set_id}/regression-runs/{eval_run_id}/review",
        response_model=EvalRunResponse,
        summary="Apply an audited human review to the current regression EvalRun",
    )
    async def review_agent_change_set_regression(
        change_set_id: str,
        eval_run_id: str,
        req: AgentChangeSetRegressionReviewRequest,
    ) -> EvalRunResponse:
        reviewed = agent_governance.review_regression(
            change_set_id,
            eval_run_id=eval_run_id,
            review_id=req.review_id,
            operator=req.operator,
            reason=req.reason,
            scope=req.scope,
            items=[item.model_dump(mode="json") for item in req.decisions],
        )
        return EvalRunResponse.model_validate(reviewed)

    return router


def _require_candidate_commit(change_set: JsonObject) -> str:
    candidate = change_set.get("candidate_commit_sha")
    if not isinstance(candidate, str) or not candidate:
        raise_conflict("Agent change set has no candidate commit")
    return candidate
