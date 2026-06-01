from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found
from app.runtime.schemas import (
    EvalCaseGovernanceEventResponse,
    EvalCaseResponse,
    EvalCaseRevisionResponse,
    FeedbackEvalCaseUpdateRequest,
    RegressionAssetFlakyRequest,
    RegressionAssetGovernanceActionRequest,
    RegressionAssetSupersedeRequest,
)
from app.runtime.stores.feedback_store import FeedbackStore


def create_regression_assets_router(
    *,
    feedback_store: FeedbackStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_regression_asset_read_routes(router, feedback_store)
    _register_regression_asset_write_routes(router, feedback_store)
    _register_regression_asset_governance_routes(router, feedback_store)
    _register_regression_asset_history_routes(router, feedback_store)
    return router


def _register_regression_asset_read_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:
    @router.get(
        "/regression-assets",
        response_model=list[EvalCaseResponse],
        summary="List governed regression assets",
    )
    async def list_regression_assets(
        status: str | None = None,
        asset_layer: str | None = None,
        promotion_status: str | None = None,
        blocking_policy: str | None = None,
        scenario_pack: str | None = None,
        flaky_status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_eval_cases(
            status=status,
            asset_layer=asset_layer,
            promotion_status=promotion_status,
            blocking_policy=blocking_policy,
            scenario_pack=scenario_pack,
            flaky_status=flaky_status,
            limit=limit,
        )

    @router.get(
        "/regression-assets/{eval_case_id}",
        response_model=EvalCaseResponse,
        summary="Get one governed regression asset",
    )
    async def get_regression_asset(eval_case_id: str) -> dict[str, Any]:
        return ensure_found(feedback_store.find_eval_case(eval_case_id), "Regression asset not found")


def _register_regression_asset_write_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:
    @router.patch(
        "/regression-assets/{eval_case_id}",
        response_model=EvalCaseResponse,
        summary="Update one governed regression asset",
    )
    async def update_regression_asset(eval_case_id: str, req: FeedbackEvalCaseUpdateRequest) -> dict[str, Any]:
        updated = feedback_store.update_eval_case(eval_case_id, req.model_dump(exclude_unset=True))
        return ensure_found(updated, "Regression asset not found")


def _register_regression_asset_governance_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:
    @router.post(
        "/regression-assets/{eval_case_id}/promote",
        response_model=EvalCaseResponse,
        summary="Promote one regression asset into the approved long-term suite",
    )
    async def promote_regression_asset(eval_case_id: str, req: RegressionAssetGovernanceActionRequest) -> dict[str, Any]:
        updated = feedback_store.promote_eval_case(eval_case_id, req.model_dump(exclude_none=True))
        return ensure_found(updated, "Regression asset not found")

    @router.post(
        "/regression-assets/{eval_case_id}/archive",
        response_model=EvalCaseResponse,
        summary="Archive one regression asset",
    )
    async def archive_regression_asset(eval_case_id: str, req: RegressionAssetGovernanceActionRequest) -> dict[str, Any]:
        updated = feedback_store.archive_eval_case(eval_case_id, req.model_dump(exclude_none=True))
        return ensure_found(updated, "Regression asset not found")

    @router.post(
        "/regression-assets/{eval_case_id}/mark-flaky",
        response_model=EvalCaseResponse,
        summary="Mark one regression asset as flaky",
    )
    async def mark_regression_asset_flaky(eval_case_id: str, req: RegressionAssetFlakyRequest) -> dict[str, Any]:
        updated = feedback_store.mark_eval_case_flaky(eval_case_id, req.model_dump(exclude_none=True), flaky=True)
        return ensure_found(updated, "Regression asset not found")

    @router.post(
        "/regression-assets/{eval_case_id}/unmark-flaky",
        response_model=EvalCaseResponse,
        summary="Mark one regression asset as stable",
    )
    async def unmark_regression_asset_flaky(eval_case_id: str, req: RegressionAssetFlakyRequest) -> dict[str, Any]:
        updated = feedback_store.mark_eval_case_flaky(eval_case_id, req.model_dump(exclude_none=True), flaky=False)
        return ensure_found(updated, "Regression asset not found")

    @router.post(
        "/regression-assets/{eval_case_id}/supersede",
        response_model=EvalCaseResponse,
        summary="Supersede one regression asset with another asset",
    )
    async def supersede_regression_asset(eval_case_id: str, req: RegressionAssetSupersedeRequest) -> dict[str, Any]:
        updated = feedback_store.supersede_eval_case(eval_case_id, req.model_dump(exclude_none=True))
        return ensure_found(updated, "Regression asset not found")


def _register_regression_asset_history_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:
    @router.get(
        "/regression-assets/{eval_case_id}/revisions",
        response_model=list[EvalCaseRevisionResponse],
        summary="List immutable revisions for one regression asset",
    )
    async def list_regression_asset_revisions(eval_case_id: str) -> list[dict[str, Any]]:
        ensure_found(feedback_store.find_eval_case(eval_case_id), "Regression asset not found")
        return feedback_store.list_eval_case_revisions(eval_case_id)

    @router.get(
        "/regression-assets/{eval_case_id}/governance-events",
        response_model=list[EvalCaseGovernanceEventResponse],
        summary="List governance audit events for one regression asset",
    )
    async def list_regression_asset_governance_events(eval_case_id: str) -> list[dict[str, Any]]:
        ensure_found(feedback_store.find_eval_case(eval_case_id), "Regression asset not found")
        return feedback_store.list_eval_case_governance_events(eval_case_id)
