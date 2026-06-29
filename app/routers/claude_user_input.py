from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.runtime.claude_user_input_schemas import (
    ClaudeUserInputDecisionRequest,
    ClaudeUserInputDecisionResponse,
    ClaudeUserInputRequestListResponse,
    ClaudeUserInputRequestResponse,
)
from app.runtime.claude_user_input_service import (
    ClaudeUserInputConflict,
    ClaudeUserInputInvalid,
    ClaudeUserInputNotFound,
    ClaudeUserInputService,
)
from app.runtime.records.claude_user_input_records import ClaudeUserInputRequestRecord


def _response(record: ClaudeUserInputRequestRecord) -> ClaudeUserInputRequestResponse:
    return ClaudeUserInputRequestResponse(**record.public_payload())


def create_claude_user_input_router(
    *,
    service: ClaudeUserInputService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["claude-hitl"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/claude-hitl-requests",
        response_model=ClaudeUserInputRequestListResponse,
        summary="List Claude SDK HITL requests for Playground Web confirmation",
    )
    async def list_requests(
        session_id: str | None = Query(default=None),
        run_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        business_agent_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> ClaudeUserInputRequestListResponse:
        return ClaudeUserInputRequestListResponse(
            requests=[
                _response(record)
                for record in service.list_requests(
                    session_id=session_id,
                    run_id=run_id,
                    status=status,
                    business_agent_id=business_agent_id,
                    limit=limit,
                )
            ]
        )

    @router.post(
        "/claude-hitl-requests/{request_id}/decision",
        response_model=ClaudeUserInputDecisionResponse,
        summary="Resolve one active Claude SDK HITL request",
    )
    async def decide(request_id: str, req: ClaudeUserInputDecisionRequest) -> ClaudeUserInputDecisionResponse:
        try:
            record = service.submit_decision(request_id, decision=req, decided_by="api_key_client")
        except ClaudeUserInputNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ClaudeUserInputConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ClaudeUserInputInvalid as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return ClaudeUserInputDecisionResponse(
            request_id=record.request_id,
            status=record.status,  # type: ignore[arg-type]
            decision=record.decision or "",
            resolved_at=record.resolved_at,
        )

    return router
