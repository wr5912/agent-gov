from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.runtime.asset_schemas import AssetCreateRequest, AssetInheritRequest, AssetResponse
from app.runtime.errors import NotFoundError
from app.runtime.stores.asset_store import AssetRecord, AssetStore
from app.runtime.test_dataset_schemas import (
    TestDatasetAdoptRequest,
    TestDatasetLifecycleRequest,
    TestDatasetResponse,
    TestDatasetRevisionResponse,
)


def _response(record: AssetRecord) -> AssetResponse:
    return AssetResponse(
        asset_id=record.asset_id,
        agent_id=record.agent_id,
        asset_type=record.asset_type,
        title=record.title,
        body=record.body,
        source_improvement_id=record.source_improvement_id,
        inherited_from=record.inherited_from,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def create_assets_router(*, asset_store: AssetStore, require_api_key: Callable) -> APIRouter:
    """治理资产 Registry 复利中心（四阶段改进治理 W3）：沉淀、查询、跨 Agent 继承复用。"""
    router = APIRouter(prefix="/api", tags=["assets"], dependencies=[Depends(require_api_key)])
    _register_generic_asset_routes(router, asset_store)
    _register_test_dataset_query_routes(router, asset_store)
    _register_test_dataset_command_routes(router, asset_store)
    return router


def _register_generic_asset_routes(router: APIRouter, asset_store: AssetStore) -> None:

    @router.get("/assets", response_model=list[AssetResponse], summary="List governance assets, scoped by agent / type")
    async def list_assets(
        agent_id: str | None = Query(default=None, description="按业务 Agent 过滤。"),
        asset_type: str | None = Query(default=None, description="按资产类型过滤。"),
        source_improvement_id: str | None = Query(default=None, description="按沉淀来源改进事项过滤（§11.2 本事项沉淀资产）。"),
    ) -> list[AssetResponse]:
        return [_response(record) for record in asset_store.list_assets(agent_id=agent_id, asset_type=asset_type, source_improvement_id=source_improvement_id)]

    @router.post("/assets", response_model=AssetResponse, status_code=201, summary="Create a governance asset")
    async def create_asset(req: AssetCreateRequest) -> AssetResponse:
        return _response(
            asset_store.create_asset(
                agent_id=req.agent_id,
                asset_type=req.asset_type,
                title=req.title,
                body=req.body,
                source_improvement_id=req.source_improvement_id,
            )
        )

    @router.get("/assets/{asset_id}", response_model=AssetResponse, summary="Get one asset (404 if unknown)")
    async def get_asset(asset_id: str) -> AssetResponse:
        record = asset_store.get_asset(asset_id)
        if record is None:
            raise NotFoundError(f"Asset not found: {asset_id}")
        return _response(record)

    @router.post(
        "/assets/{asset_id}/inherit",
        response_model=AssetResponse,
        status_code=201,
        summary="Inherit (compound) an asset into another business agent",
    )
    async def inherit_asset(asset_id: str, req: AssetInheritRequest) -> AssetResponse:
        # 未知资产 404；目标 Agent 已拥有 / 空目标 400。
        return _response(asset_store.inherit_asset(asset_id, target_agent_id=req.target_agent_id))


def _register_test_dataset_query_routes(router: APIRouter, asset_store: AssetStore) -> None:
    @router.get(
        "/test-datasets",
        response_model=list[TestDatasetResponse],
        summary="List typed TestDatasets for one business agent",
    )
    async def list_test_datasets(
        agent_id: str = Query(description="权威业务 Agent 归属。"),
        source_improvement_id: str | None = Query(default=None, description="按来源改进事项过滤。"),
    ) -> list[TestDatasetResponse]:
        return asset_store.test_datasets.list_datasets(
            agent_id=agent_id,
            source_improvement_id=source_improvement_id,
        )

    @router.get(
        "/test-datasets/{dataset_id}",
        response_model=TestDatasetResponse,
        summary="Get one typed TestDataset in an Agent scope",
    )
    async def get_test_dataset(
        dataset_id: str,
        agent_id: str = Query(description="权威业务 Agent 归属。"),
    ) -> TestDatasetResponse:
        return asset_store.test_datasets.get_dataset(dataset_id, agent_id=agent_id)

    @router.get(
        "/test-datasets/{dataset_id}/revisions",
        response_model=list[TestDatasetRevisionResponse],
        summary="List immutable lifecycle revisions for one typed TestDataset",
    )
    async def list_test_dataset_revisions(
        dataset_id: str,
        agent_id: str = Query(description="权威业务 Agent 归属。"),
    ) -> list[TestDatasetRevisionResponse]:
        return asset_store.test_datasets.list_revisions(dataset_id, agent_id=agent_id)


def _register_test_dataset_command_routes(router: APIRouter, asset_store: AssetStore) -> None:
    @router.post(
        "/improvements/{improvement_id}/test-dataset/adopt",
        response_model=TestDatasetResponse,
        summary="Adopt a typed TestDataset from a confirmed improvement regression chain",
    )
    async def adopt_test_dataset(
        improvement_id: str,
        _: TestDatasetAdoptRequest,
    ) -> TestDatasetResponse:
        return asset_store.test_datasets.adopt_from_improvement(improvement_id)

    @router.post(
        "/test-datasets/{dataset_id}/lifecycle",
        response_model=TestDatasetResponse,
        summary="Transition a TestDataset through its centralized lifecycle",
    )
    async def transition_test_dataset_lifecycle(
        dataset_id: str,
        req: TestDatasetLifecycleRequest,
        agent_id: str = Query(description="权威业务 Agent 归属。"),
    ) -> TestDatasetResponse:
        return asset_store.test_datasets.transition_lifecycle(
            dataset_id,
            agent_id=agent_id,
            target_state=req.target_state,
            expected_revision=req.expected_revision,
            operator=req.operator,
            reason=req.reason,
        )
