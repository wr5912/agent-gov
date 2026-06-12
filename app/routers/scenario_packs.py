from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.agent_governance_schemas import ScenarioPackCreateRequest, ScenarioPackResponse
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

    return router
