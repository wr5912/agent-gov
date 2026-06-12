from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.agent_governance_schemas import (
    ScenarioPackAssociateRequest,
    ScenarioPackCopyRequest,
    ScenarioPackCreateRequest,
    ScenarioPackResponse,
)
from app.runtime.stores.scenario_pack_store import ScenarioPackRecord, ScenarioPackStore


def _summary(record: ScenarioPackRecord) -> ScenarioPackResponse:
    return ScenarioPackResponse(
        scenario_pack_id=record.scenario_pack_id,
        name=record.name,
        business_goal=record.business_goal,
        scope=record.scope,
        risk_level=record.risk_level,
        created_at=record.created_at,
        agent_ids=record.agent_ids,
        eval_case_ids=record.eval_case_ids,
        asset_refs=record.asset_refs,
    )


def create_scenario_packs_router(*, scenario_pack_store: ScenarioPackStore, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["scenario-packs"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/scenario-packs",
        response_model=ScenarioPackResponse,
        status_code=201,
        summary="Create a scenario pack (capability domain) organizing governance assets",
    )
    async def create_pack(req: ScenarioPackCreateRequest) -> ScenarioPackResponse:
        return _summary(
            scenario_pack_store.create_scenario_pack(
                name=req.name, business_goal=req.business_goal, scope=req.scope, risk_level=req.risk_level
            )
        )

    @router.get("/scenario-packs", response_model=list[ScenarioPackResponse], summary="List scenario packs")
    async def list_packs() -> list[ScenarioPackResponse]:
        return [_summary(record) for record in scenario_pack_store.list_scenario_packs()]

    @router.get(
        "/scenario-packs/{scenario_pack_id}",
        response_model=ScenarioPackResponse,
        summary="Get one scenario pack with its asset relationships",
    )
    async def get_pack(scenario_pack_id: str) -> ScenarioPackResponse:
        return _summary(scenario_pack_store.get_scenario_pack(scenario_pack_id))

    @router.post(
        "/scenario-packs/{scenario_pack_id}/assets",
        response_model=ScenarioPackResponse,
        summary="Associate agents/eval-cases/assets to a scenario pack (capability assembly)",
    )
    async def associate_assets(scenario_pack_id: str, req: ScenarioPackAssociateRequest) -> ScenarioPackResponse:
        # AGV-026 criterion 3：Agent 据此装配场景包能力；关联去重并集、可审计。
        return _summary(
            scenario_pack_store.associate_scenario_pack_assets(
                scenario_pack_id, agent_ids=req.agent_ids, eval_case_ids=req.eval_case_ids, asset_refs=req.asset_refs
            )
        )

    @router.post(
        "/scenario-packs/{scenario_pack_id}/copy",
        response_model=ScenarioPackResponse,
        status_code=201,
        summary="Copy a scenario pack as a reusable template (assets migratable/copyable)",
    )
    async def copy_pack(scenario_pack_id: str, req: ScenarioPackCopyRequest) -> ScenarioPackResponse:
        # AGV-026 criterion 2：资产可复制/迁移；新包作为模板，各 Agent 另行装配保留审计边界（AGV-027）。
        return _summary(scenario_pack_store.copy_scenario_pack(scenario_pack_id, name=req.name))

    return router
