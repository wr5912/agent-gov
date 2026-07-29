from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.runtime.run_control import AgentRunCancelResponse, RunCancellationService


def create_agent_run_control_router(
    *,
    cancellation_service: RunCancellationService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-runs",
        tags=["agent-run-control"],
        dependencies=[Depends(require_api_key)],
    )

    @router.post(
        "/{run_id}/cancel",
        response_model=AgentRunCancelResponse,
        summary="Cancel one exact managed Agent run and wait for durable termination",
        description=("Returns only after the target run is terminal and no longer owns its session fence. Repeated cancellation is idempotent."),
        responses={
            404: {"description": "The persisted Agent run does not exist."},
            409: {"description": "The running turn has no owner in this API process or its fence is inconsistent."},
            504: {"description": "Cancellation continues, but durable termination was not confirmed before the timeout."},
        },
    )
    async def cancel_agent_run(
        run_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> AgentRunCancelResponse:
        return await cancellation_service.cancel(run_id)

    return router
