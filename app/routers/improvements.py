from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.runtime.errors import NotFoundError
from app.runtime.improvement_schemas import (
    ImprovementCreateRequest,
    ImprovementItemResponse,
    ImprovementStageTransitionRequest,
)
from app.runtime.stores.improvement_store import ImprovementItemRecord, ImprovementStore


def _response(record: ImprovementItemRecord) -> ImprovementItemResponse:
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


def create_improvements_router(
    *,
    improvement_store: ImprovementStore,
    require_api_key: Callable,
) -> APIRouter:
    """改进事项 ImprovementItem 路由（v2.7 跨代重建：事项级单一领域实体）。

    统一术语：资源 /improvements、ID improvement_id、阶段 improvement_stage。无旧名/无双轨。
    """
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/improvements",
        response_model=list[ImprovementItemResponse],
        summary="List improvement items (governance work units), scoped by business agent",
    )
    async def list_improvements(agent_id: str | None = Query(default=None, description="按业务 Agent 归属过滤；省略则返回全部 Agent。")) -> list[ImprovementItemResponse]:
        return [_response(record) for record in improvement_store.list_improvements(agent_id=agent_id)]

    @router.post(
        "/improvements",
        response_model=ImprovementItemResponse,
        status_code=201,
        summary="Create an improvement item under a business agent",
    )
    async def create_improvement(req: ImprovementCreateRequest) -> ImprovementItemResponse:
        # backend-owned improvement_id/stage/status 由后端生成，不接受请求覆盖（字段所有权）。
        record = improvement_store.create_improvement(
            agent_id=req.agent_id,
            title=req.title,
            summary=req.summary,
            source_feedback_refs=req.source_feedback_refs,
        )
        return _response(record)

    @router.get(
        "/improvements/{improvement_id}",
        response_model=ImprovementItemResponse,
        summary="Get one improvement item (404 if unknown)",
    )
    async def get_improvement(improvement_id: str) -> ImprovementItemResponse:
        record = improvement_store.get_improvement(improvement_id)
        if record is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _response(record)

    @router.post(
        "/improvements/{improvement_id}/lifecycle",
        response_model=ImprovementItemResponse,
        summary="Transition an improvement item's stage (rejects illegal transitions with 409)",
    )
    async def transition_improvement(improvement_id: str, req: ImprovementStageTransitionRequest) -> ImprovementItemResponse:
        # 合法阶段转移由集中状态机 improvement_stage 判定；非法转移 / 已归档返回 409。
        return _response(improvement_store.transition_stage(improvement_id, stage=req.stage))

    @router.post(
        "/improvements/{improvement_id}/archive",
        response_model=ImprovementItemResponse,
        summary="Archive an improvement item (terminal status archived; no further stage transitions)",
    )
    async def archive_improvement(improvement_id: str) -> ImprovementItemResponse:
        # 归档为终态状态：improvement_status=archived，归档后阶段转移被拒（409）。
        return _response(improvement_store.archive_improvement(improvement_id))

    return router
