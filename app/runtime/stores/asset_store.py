from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from ..asset_db import AssetModel
from ..errors import BusinessRuleViolation, NotFoundError
from ..runtime_db import utc_now

ASSET_TYPES = {"methodology", "execution", "audit"}


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    agent_id: str
    asset_type: str
    title: str
    body: str
    source_improvement_id: str
    inherited_from: str
    created_at: str
    updated_at: str


class AssetStore:
    """治理资产 Registry：按 agent / type 查询，支持跨 Agent 继承复用（复利）。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def list_assets(
        self,
        *,
        agent_id: str | None = None,
        asset_type: str | None = None,
        source_improvement_id: str | None = None,
    ) -> list[AssetRecord]:
        if asset_type and asset_type not in ASSET_TYPES:
            raise BusinessRuleViolation(f"Unknown asset_type: {asset_type}; expected one of {sorted(ASSET_TYPES)}")
        with self._session_factory.begin() as db:
            query = db.query(AssetModel).filter(AssetModel.asset_type.in_(ASSET_TYPES))
            if agent_id:
                query = query.filter(AssetModel.agent_id == agent_id)
            if asset_type:
                query = query.filter(AssetModel.asset_type == asset_type)
            if source_improvement_id:
                query = query.filter(AssetModel.source_improvement_id == source_improvement_id)
            rows = query.order_by(AssetModel.created_at.desc(), AssetModel.asset_id).all()
            return [_record(row) for row in rows]

    def get_asset(self, asset_id: str) -> AssetRecord | None:
        with self._session_factory.begin() as db:
            row = db.get(AssetModel, asset_id)
            return _record(row) if row is not None and row.asset_type in ASSET_TYPES else None

    def create_asset(
        self,
        *,
        agent_id: str,
        asset_type: str,
        title: str,
        body: str = "",
        source_improvement_id: str = "",
    ) -> AssetRecord:
        clean_agent = (agent_id or "").strip()
        clean_title = (title or "").strip()
        if not clean_agent:
            raise BusinessRuleViolation("Asset must belong to a business agent (agent_id required)")
        if asset_type not in ASSET_TYPES:
            raise BusinessRuleViolation(f"Unknown asset_type: {asset_type}; expected one of {sorted(ASSET_TYPES)}")
        if not clean_title:
            raise BusinessRuleViolation("Asset title cannot be empty")
        return self._insert(
            agent_id=clean_agent,
            asset_type=asset_type,
            title=clean_title,
            body=body or "",
            source_improvement_id=(source_improvement_id or "").strip(),
            inherited_from="",
        )

    def inherit_asset(self, asset_id: str, *, target_agent_id: str) -> AssetRecord:
        """把资产继承复用到另一个业务 Agent：复制为目标 Agent 名下的新资产，记录 inherited_from。"""
        clean_target = (target_agent_id or "").strip()
        if not clean_target:
            raise BusinessRuleViolation("target_agent_id is required to inherit an asset")
        source = self.get_asset(asset_id)
        if source is None:
            raise NotFoundError(f"Asset not found: {asset_id}")
        if source.agent_id == clean_target:
            raise BusinessRuleViolation("Target agent already owns this asset")
        return self._insert(
            agent_id=clean_target,
            asset_type=source.asset_type,
            title=source.title,
            body=source.body,
            source_improvement_id=source.source_improvement_id,
            inherited_from=source.asset_id,
        )

    def _insert(
        self,
        *,
        agent_id: str,
        asset_type: str,
        title: str,
        body: str,
        source_improvement_id: str,
        inherited_from: str,
    ) -> AssetRecord:
        asset_id = f"ast-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            db.add(
                AssetModel(
                    asset_id=asset_id,
                    agent_id=agent_id,
                    asset_type=asset_type,
                    title=title,
                    body=body,
                    source_improvement_id=source_improvement_id,
                    inherited_from=inherited_from,
                    created_at=now,
                    updated_at=now,
                )
            )
        return AssetRecord(
            asset_id=asset_id,
            agent_id=agent_id,
            asset_type=asset_type,
            title=title,
            body=body,
            source_improvement_id=source_improvement_id,
            inherited_from=inherited_from,
            created_at=now,
            updated_at=now,
        )


def _record(row: AssetModel) -> AssetRecord:
    return AssetRecord(
        asset_id=row.asset_id,
        agent_id=row.agent_id,
        asset_type=row.asset_type,
        title=row.title,
        body=row.body or "",
        source_improvement_id=row.source_improvement_id or "",
        inherited_from=row.inherited_from or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
