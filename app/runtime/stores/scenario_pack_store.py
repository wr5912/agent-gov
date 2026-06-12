from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, NotFoundError
from ..runtime_db import utc_now
from ..scenario_pack_db import ScenarioPackModel

_RISK_LEVELS = {"low", "medium", "high"}


@dataclass(frozen=True)
class ScenarioPackRecord:
    """场景包/能力域记录（AGV-026/027）。"""

    scenario_pack_id: str
    name: str
    business_goal: str
    scope: str
    risk_level: str
    created_at: str
    agent_ids: list[str] = field(default_factory=list)
    eval_case_ids: list[str] = field(default_factory=list)
    asset_refs: list[str] = field(default_factory=list)


def _record(row: ScenarioPackModel) -> ScenarioPackRecord:
    payload = dict(row.payload_json or {})
    return ScenarioPackRecord(
        scenario_pack_id=row.scenario_pack_id,
        name=row.name,
        business_goal=row.business_goal or "",
        scope=row.scope or "",
        risk_level=row.risk_level or "medium",
        created_at=row.created_at,
        agent_ids=list(payload.get("agent_ids") or []),
        eval_case_ids=list(payload.get("eval_case_ids") or []),
        asset_refs=list(payload.get("asset_refs") or []),
    )


class ScenarioPackStore:
    """场景包存储（AGV-026/027）：按业务场景组织治理资产的一等对象。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create_scenario_pack(
        self, *, name: str, business_goal: str = "", scope: str = "", risk_level: str = "medium"
    ) -> ScenarioPackRecord:
        clean_name = (name or "").strip()
        if not clean_name:
            raise BusinessRuleViolation("Scenario pack name cannot be empty")
        if risk_level not in _RISK_LEVELS:
            raise BusinessRuleViolation(f"Unsupported risk_level: {risk_level}; expected one of {sorted(_RISK_LEVELS)}")
        scenario_pack_id = f"pack-{uuid.uuid4().hex[:12]}"
        with self._session_factory.begin() as db:
            row = ScenarioPackModel(
                scenario_pack_id=scenario_pack_id,
                name=clean_name,
                business_goal=business_goal or "",
                scope=scope or "",
                risk_level=risk_level,
                created_at=utc_now(),
                payload_json={"agent_ids": [], "eval_case_ids": [], "asset_refs": []},
            )
            db.add(row)
            return _record(row)

    def get_scenario_pack(self, scenario_pack_id: str) -> ScenarioPackRecord:
        with self._session_factory.begin() as db:
            row = db.get(ScenarioPackModel, scenario_pack_id)
            if row is None:
                raise NotFoundError(f"Scenario pack not found: {scenario_pack_id}")
            return _record(row)

    def list_scenario_packs(self) -> list[ScenarioPackRecord]:
        with self._session_factory.begin() as db:
            rows = db.query(ScenarioPackModel).order_by(ScenarioPackModel.created_at, ScenarioPackModel.scenario_pack_id).all()
            return [_record(row) for row in rows]
