from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.runtime.automation_schemas import (
    AutoAdvanceResponse,
    AutomationPolicyResponse,
    AutomationPolicyUpdateRequest,
)
from app.runtime.errors import NotFoundError
from app.runtime.improvement_schemas import ImprovementItemResponse
from app.runtime.stores.automation_policy_store import AutomationPolicyStore
from app.runtime.stores.improvement_store import ImprovementItemRecord, ImprovementStore
from app.services.improvement_automation import auto_advance


def _improvement_response(record: ImprovementItemRecord) -> ImprovementItemResponse:
    return ImprovementItemResponse(
        improvement_id=record.improvement_id,
        agent_id=record.agent_id,
        title=record.title,
        summary=record.summary,
        source_feedback_refs=list(record.source_feedback_refs),
        improvement_stage=record.improvement_stage,
        improvement_status=record.improvement_status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def create_automation_router(
    *,
    improvement_store: ImprovementStore,
    automation_policy_store: AutomationPolicyStore,
    require_api_key: Callable,
) -> APIRouter:
    """自动化策略编排（四阶段改进治理 W2）：策略读写 + 改进事项按策略自动推进。"""
    router = APIRouter(prefix="/api", tags=["automation"], dependencies=[Depends(require_api_key)])

    @router.get("/automation-policy", response_model=AutomationPolicyResponse, summary="Get automation policy for a business agent")
    async def get_policy(agent_id: str = Query(description="业务 Agent ID")) -> AutomationPolicyResponse:
        return AutomationPolicyResponse(agent_id=agent_id, mode=automation_policy_store.get_mode(agent_id))

    @router.put("/automation-policy", response_model=AutomationPolicyResponse, summary="Set automation policy mode (off/semi/full)")
    async def put_policy(req: AutomationPolicyUpdateRequest) -> AutomationPolicyResponse:
        mode = automation_policy_store.set_mode(req.agent_id, mode=req.mode)
        return AutomationPolicyResponse(agent_id=req.agent_id, mode=mode)

    @router.post(
        "/improvements/{improvement_id}/auto-advance",
        response_model=AutoAdvanceResponse,
        summary="Auto-advance an improvement under its agent's automation policy",
    )
    async def auto_advance_improvement(improvement_id: str) -> AutoAdvanceResponse:
        item = improvement_store.get_improvement(improvement_id)
        if item is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        mode = automation_policy_store.get_mode(item.agent_id)
        result = auto_advance(improvement_store, mode=mode, item=item)
        return AutoAdvanceResponse(
            improvement=_improvement_response(result.item),
            applied_stages=result.applied_stages,
            stopped_reason=result.stopped_reason,
        )

    return router
