from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

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
    merged_into: Optional[str] = None


@dataclass(frozen=True)
class DuplicateScenarioPackGroup:
    """一组重名（规范化后）的疑似重复场景包及合并建议（AGV-023）。"""

    normalized_name: str
    scenario_pack_ids: list[str]
    suggested_primary_id: str


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
        merged_into=payload.get("merged_into"),
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

    def detect_duplicate_scenario_packs(self) -> list[DuplicateScenarioPackGroup]:
        """按规范化名称检测重复场景包，给出合并治理建议（AGV-023 criterion 1）。

        已合并（merged_into 非空）的包不再参与检测；每组建议以最早创建者为主资产。
        """
        groups: dict[str, list[ScenarioPackRecord]] = {}
        for record in self.list_scenario_packs():
            if record.merged_into:
                continue
            key = record.name.strip().lower()
            groups.setdefault(key, []).append(record)
        duplicates: list[DuplicateScenarioPackGroup] = []
        for key, records in groups.items():
            if len(records) < 2:
                continue
            ordered = sorted(records, key=lambda r: (r.created_at, r.scenario_pack_id))
            duplicates.append(
                DuplicateScenarioPackGroup(
                    normalized_name=key,
                    scenario_pack_ids=[r.scenario_pack_id for r in ordered],
                    suggested_primary_id=ordered[0].scenario_pack_id,
                )
            )
        return duplicates

    def merge_scenario_packs(self, primary_id: str, *, duplicate_ids: list[str]) -> ScenarioPackRecord:
        """把重复场景包并入主资产（AGV-023 criterion 2/3）。

        主资产并入各重复包的关联（agent_ids/eval_case_ids/asset_refs，去重并集），重复包标记
        merged_into=主资产并保留（不物理删除，可审计；引用经 merged_into 重定向到主资产，不丢失）。
        """
        unique_dups = [d for d in dict.fromkeys(duplicate_ids) if d and d != primary_id]
        if not unique_dups:
            raise BusinessRuleViolation("merge requires at least one duplicate distinct from the primary")
        with self._session_factory.begin() as db:
            primary = db.get(ScenarioPackModel, primary_id)
            if primary is None:
                raise NotFoundError(f"Scenario pack not found: {primary_id}")
            primary_payload = dict(primary.payload_json or {})
            for dup_id in unique_dups:
                dup = db.get(ScenarioPackModel, dup_id)
                if dup is None:
                    raise NotFoundError(f"Scenario pack not found: {dup_id}")
                dup_payload = dict(dup.payload_json or {})
                for key in ("agent_ids", "eval_case_ids", "asset_refs"):
                    primary_payload[key] = list(
                        dict.fromkeys([*(primary_payload.get(key) or []), *(dup_payload.get(key) or [])])
                    )
                dup_payload["merged_into"] = primary_id
                dup_payload["merged_at"] = utc_now()
                dup.payload_json = dup_payload
            primary.payload_json = primary_payload
            return _record(primary)

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

    def associate_scenario_pack_assets(
        self,
        scenario_pack_id: str,
        *,
        agent_ids: list[str] | None = None,
        eval_case_ids: list[str] | None = None,
        asset_refs: list[str] | None = None,
    ) -> ScenarioPackRecord:
        """把资产/Agent 关联到场景包（去重并集）。Agent 据此装配该场景包能力（AGV-026 criterion 3）。"""
        with self._session_factory.begin() as db:
            row = db.get(ScenarioPackModel, scenario_pack_id)
            if row is None:
                raise NotFoundError(f"Scenario pack not found: {scenario_pack_id}")
            payload = dict(row.payload_json or {})
            for key, values in (("agent_ids", agent_ids), ("eval_case_ids", eval_case_ids), ("asset_refs", asset_refs)):
                if values:
                    payload[key] = list(dict.fromkeys([*(payload.get(key) or []), *values]))
            row.payload_json = payload
            return _record(row)

    def copy_scenario_pack(self, scenario_pack_id: str, *, name: str) -> ScenarioPackRecord:
        """复制一个场景包为新包（AGV-026 criterion 2：资产可复制/迁移）。

        复制业务目标、适用范围、风险等级与资产引用（eval_case_ids/asset_refs）；不复制 agent_ids，
        新包作为模板由各 Agent 另行装配，保留各自审计边界（AGV-027）。
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise BusinessRuleViolation("Scenario pack name cannot be empty")
        with self._session_factory.begin() as db:
            source = db.get(ScenarioPackModel, scenario_pack_id)
            if source is None:
                raise NotFoundError(f"Scenario pack not found: {scenario_pack_id}")
            source_payload = dict(source.payload_json or {})
            new_id = f"pack-{uuid.uuid4().hex[:12]}"
            row = ScenarioPackModel(
                scenario_pack_id=new_id,
                name=clean_name,
                business_goal=source.business_goal or "",
                scope=source.scope or "",
                risk_level=source.risk_level or "medium",
                created_at=utc_now(),
                payload_json={
                    "agent_ids": [],
                    "eval_case_ids": list(source_payload.get("eval_case_ids") or []),
                    "asset_refs": list(source_payload.get("asset_refs") or []),
                    "copied_from": scenario_pack_id,
                },
            )
            db.add(row)
            return _record(row)
