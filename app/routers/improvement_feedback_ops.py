from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.errors import BusinessRuleViolation, ConflictError, NotFoundError
from app.runtime.improvement_content_schemas import (
    AttachableFeedbackCase,
    AttachableFeedbacksResponse,
    AttachFeedbackCaseRequest,
    ImprovementDeletionImpactResponse,
    ImprovementFeedbackReassignRequest,
    ImprovementFeedbackResponse,
)
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_content_store import ImprovementContentStore, ImprovementFeedbackRecord
from app.runtime.stores.improvement_store import ImprovementStore


def _fb_response(r: ImprovementFeedbackRecord) -> ImprovementFeedbackResponse:
    return ImprovementFeedbackResponse(
        feedback_id=r.feedback_id, improvement_id=r.improvement_id, agent_id=r.agent_id, summary=r.summary,
        source=r.source, status=r.status, raw_text=r.raw_text, run_id=r.run_id, session_id=r.session_id,
        agent_version_id=r.agent_version_id, scenario=r.scenario, task_id=r.task_id,
        alert_id=r.alert_id, case_id=r.case_id, created_at=r.created_at,
    )


def create_improvement_feedback_ops_router(
    *,
    improvement_store: ImprovementStore,
    content_store: ImprovementContentStore,
    feedback_store: FeedbackStore,
    require_api_key: Callable,
) -> APIRouter:
    """Part B：改进事项「选择已有反馈 / 跨事项调整 / 删除事项」操作路由。

    与 create_improvement_content_router 分离以保持单文件路由数与单函数体量在治理阈值内。
    """
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    @router.get("/improvements/{improvement_id}/attachable-feedbacks", response_model=AttachableFeedbacksResponse, summary="List feedback selectable to add: unassigned FeedbackCases + other improvements' feedbacks")
    async def attachable_feedbacks(improvement_id: str) -> AttachableFeedbacksResponse:
        item = improvement_store.get_improvement(improvement_id)
        if item is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        assigned = improvement_store.all_source_feedback_refs()
        cases = [
            AttachableFeedbackCase(
                feedback_case_id=str(c.get("feedback_case_id") or ""), title=str(c.get("title") or ""),
                status=str(c.get("status") or ""), run_ids=[str(r) for r in (c.get("run_ids") or [])],
            )
            for c in feedback_store.list_cases()
            if str(c.get("feedback_case_id") or "") and c.get("feedback_case_id") not in assigned
        ]
        others = [_fb_response(r) for r in content_store.list_attachable_feedbacks(agent_id=item.agent_id, exclude_improvement_id=improvement_id)]
        return AttachableFeedbacksResponse(feedback_cases=cases, other_improvement_feedbacks=others)

    @router.post("/improvements/{improvement_id}/attach-feedback-case", response_model=ImprovementFeedbackResponse, status_code=201, summary="Attach an existing FeedbackCase to this improvement (prefilled + ref registered)")
    async def attach_feedback_case(improvement_id: str, req: AttachFeedbackCaseRequest) -> ImprovementFeedbackResponse:
        item = improvement_store.get_improvement(improvement_id)
        if item is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        case = feedback_store.find_case(req.feedback_case_id)
        if case is None:
            raise NotFoundError(f"FeedbackCase not found: {req.feedback_case_id}")
        run_ids = [str(r) for r in (case.get("run_ids") or [])]
        fb = content_store.create_feedback(
            improvement_id, agent_id=item.agent_id, summary=str(case.get("title") or req.feedback_case_id),
            source="feedback_inbox", run_id=run_ids[0] if run_ids else "", case_id=req.feedback_case_id,
        )
        # 真关联：把一等反馈 Case 引用登记到事项 source_feedback_refs，使其离开未归属池、可解析。
        improvement_store.add_source_refs(improvement_id, [req.feedback_case_id])
        return _fb_response(fb)

    @router.post("/improvements/{improvement_id}/feedbacks/{feedback_id}/reassign", response_model=ImprovementFeedbackResponse, summary="Move a feedback to another improvement (cross-item adjust)")
    async def reassign_feedback(improvement_id: str, feedback_id: str, req: ImprovementFeedbackReassignRequest) -> ImprovementFeedbackResponse:
        src = improvement_store.get_improvement(improvement_id)
        tgt = improvement_store.get_improvement(req.target_improvement_id)
        if src is None or tgt is None:
            raise NotFoundError("ImprovementItem not found")
        if src.agent_id != tgt.agent_id:
            raise BusinessRuleViolation("Cannot reassign feedback across different business agents")
        if tgt.improvement_status == "archived":
            raise ConflictError("Cannot reassign feedback into an archived improvement")
        return _fb_response(content_store.reassign_feedback(feedback_id, target_improvement_id=req.target_improvement_id))

    @router.get("/improvements/{improvement_id}/deletion-impact", response_model=ImprovementDeletionImpactResponse, summary="Preview impact of deleting an improvement (dry-run; FeedbackCases return to the unassigned pool)")
    async def deletion_impact(improvement_id: str) -> ImprovementDeletionImpactResponse:
        imp = improvement_store.deletion_impact(improvement_id)
        return ImprovementDeletionImpactResponse(
            improvement_id=imp.improvement_id, title=imp.title, source_feedback_refs=imp.source_feedback_refs,
            feedbacks=imp.feedbacks, links=imp.links, has_attribution=imp.has_attribution,
            has_optimization_plan=imp.has_optimization_plan,
        )

    @router.delete("/improvements/{improvement_id}", status_code=204, summary="Delete an improvement (hard delete; cascades feedbacks/content; FeedbackCases survive → unassigned pool)")
    async def delete_improvement(improvement_id: str) -> None:
        improvement_store.delete_improvement(improvement_id)

    return router
