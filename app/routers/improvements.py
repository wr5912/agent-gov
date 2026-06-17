from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.runtime.errors import NotFoundError
from app.runtime.improvement_schemas import (
    ImprovementCreateRequest,
    ImprovementItemResponse,
    ImprovementLinkRequest,
    ImprovementLinkResponse,
    ImprovementMergeRequest,
    ImprovementSimilarItem,
    ImprovementSplitRequest,
    ImprovementStageTransitionRequest,
)
from app.runtime.stores.improvement_store import ImprovementItemRecord, ImprovementLinkRecord, ImprovementStore
from app.services.improvement_similarity import find_similar_improvements


def _link_response(record: ImprovementLinkRecord) -> ImprovementLinkResponse:
    return ImprovementLinkResponse(
        link_id=record.link_id,
        improvement_id=record.improvement_id,
        kind=record.kind,
        ref_id=record.ref_id,
        created_at=record.created_at,
    )


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
        # auto_merge：同 Agent 存在相似开放事项时，把来源反馈并入该事项而非新建（W2-b 相似度归并）。
        if req.auto_merge and req.agent_id.strip():
            similar = find_similar_improvements(
                improvement_store,
                agent_id=req.agent_id,
                text=f"{req.title} {req.summary}",
                refs=req.source_feedback_refs,
            )
            if similar:
                return _response(improvement_store.add_source_refs(similar[0][0].improvement_id, req.source_feedback_refs))
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


def create_improvement_relations_router(
    *,
    improvement_store: ImprovementStore,
    require_api_key: Callable,
) -> APIRouter:
    """改进事项关系路由（v2.7 W2-b/c）：相似度归并 / 拆分 + 闭环对象轻引用。

    与 create_improvements_router 分离以保持单函数体量可控（架构卫生：单函数 ≤ 80 行）。
    """
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/improvements/{improvement_id}/similar",
        response_model=list[ImprovementSimilarItem],
        summary="Find similar open improvements under the same business agent (deterministic similarity)",
    )
    async def similar_improvements(improvement_id: str) -> list[ImprovementSimilarItem]:
        item = improvement_store.get_improvement(improvement_id)
        if item is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        results = find_similar_improvements(
            improvement_store,
            agent_id=item.agent_id,
            text=f"{item.title} {item.summary}",
            refs=item.source_feedback_refs,
            exclude_id=improvement_id,
        )
        return [ImprovementSimilarItem(improvement=_response(rec), score=score) for rec, score in results]

    @router.post(
        "/improvements/{improvement_id}/merge",
        response_model=ImprovementItemResponse,
        summary="Merge a source improvement into this one (same agent; source becomes archived)",
    )
    async def merge_improvement(improvement_id: str, req: ImprovementMergeRequest) -> ImprovementItemResponse:
        # 跨 Agent 归并 / 自归并 / 源已归档 由 store 拒绝（400/409）。
        return _response(improvement_store.merge_improvements(improvement_id, source_id=req.source_improvement_id))

    @router.post(
        "/improvements/{improvement_id}/split",
        response_model=ImprovementItemResponse,
        status_code=201,
        summary="Split a source feedback ref out of this improvement into a new one",
    )
    async def split_improvement(improvement_id: str, req: ImprovementSplitRequest) -> ImprovementItemResponse:
        return _response(improvement_store.split_improvement(improvement_id, feedback_ref=req.feedback_ref))

    @router.get(
        "/improvements/{improvement_id}/links",
        response_model=list[ImprovementLinkResponse],
        summary="List closed-loop object links of an improvement (404 if unknown)",
    )
    async def list_links(improvement_id: str) -> list[ImprovementLinkResponse]:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return [_link_response(record) for record in improvement_store.list_links(improvement_id)]

    @router.post(
        "/improvements/{improvement_id}/links",
        response_model=ImprovementLinkResponse,
        status_code=201,
        summary="Link an improvement to a closed-loop object (attribution/plan/eval/change_set/batch)",
    )
    async def add_link(improvement_id: str, req: ImprovementLinkRequest) -> ImprovementLinkResponse:
        # 未知 kind / 空 ref_id 由 store 拒绝（400）；未知改进事项 404。
        return _link_response(improvement_store.add_link(improvement_id, kind=req.kind, ref_id=req.ref_id))

    return router
