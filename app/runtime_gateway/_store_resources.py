from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from ._store_support import (
    RuntimeObjectNotFound,
    RuntimeStateConflict,
    _deletion_snapshot_id,
    _detached,
    _require_agent_deletion,
    _require_ephemeral_resource,
)
from .contracts import ACTIVE_RUN_STATUSES
from .models import (
    AgentRunModel,
    RuntimeAgentDeletionIntentModel,
    RuntimeAgentVersionModel,
    RuntimeEphemeralResourceModel,
    RuntimeSessionBindingModel,
    RuntimeSessionCreationIntentModel,
)


@dataclass(frozen=True)
class _DeletionInventory:
    versions: list[RuntimeAgentVersionModel]
    bindings: list[RuntimeSessionBindingModel]
    creations: list[RuntimeSessionCreationIntentModel]
    ephemerals: list[RuntimeEphemeralResourceModel]


def _load_deletion_inventory(db: Session, agent_id: str) -> _DeletionInventory:
    versions = list(
        db.scalars(
            select(RuntimeAgentVersionModel).where(RuntimeAgentVersionModel.governance_agent_id == agent_id).order_by(RuntimeAgentVersionModel.created_at),
        ).all(),
    )
    bindings = list(
        db.scalars(
            select(RuntimeSessionBindingModel).where(RuntimeSessionBindingModel.agent_id == agent_id).order_by(RuntimeSessionBindingModel.created_at),
        ).all(),
    )
    creations = list(
        db.scalars(
            select(RuntimeSessionCreationIntentModel)
            .where(RuntimeSessionCreationIntentModel.agent_id == agent_id)
            .order_by(RuntimeSessionCreationIntentModel.created_at),
        ).all(),
    )
    ephemerals = list(
        db.scalars(
            select(RuntimeEphemeralResourceModel)
            .where(
                RuntimeEphemeralResourceModel.business_agent_id == agent_id,
                RuntimeEphemeralResourceModel.status != "cleanup_complete",
            )
            .order_by(RuntimeEphemeralResourceModel.created_at),
        ).all(),
    )
    return _DeletionInventory(versions, bindings, creations, ephemerals)


def _deletion_version_targets(inventory: _DeletionInventory, agent_id: str) -> list[JsonObject]:
    targets: dict[str, JsonObject] = {}
    for row in inventory.versions:
        targets[row.runtime_agent_id] = {
            "runtime_agent_id": row.runtime_agent_id,
            "agent_version_id": row.agent_version_id,
            "harness_digest": row.harness_digest,
            "version_owner_id": row.agent_id,
            "source_kind": row.source_kind,
            "source_id": row.source_id,
        }
    for row in inventory.ephemerals:
        assert row.runtime_agent_id is not None
        targets.setdefault(
            row.runtime_agent_id,
            {
                "runtime_agent_id": row.runtime_agent_id,
                "agent_version_id": row.agent_version_id,
                "harness_digest": row.harness_digest,
                "version_owner_id": row.version_owner_id,
                "source_kind": row.source_kind,
                "source_id": row.source_id,
            },
        )
    for row in (*inventory.bindings, *inventory.creations):
        targets.setdefault(
            row.runtime_agent_id,
            {
                "runtime_agent_id": row.runtime_agent_id,
                "agent_version_id": row.agent_version_id,
                "harness_digest": row.harness_digest,
                "version_owner_id": agent_id,
                "source_kind": "published",
                "source_id": None,
            },
        )
    return [targets[key] for key in sorted(targets)]


def _deletion_session_targets(inventory: _DeletionInventory) -> list[JsonObject]:
    targets: dict[str, JsonObject] = {}
    for row in inventory.bindings:
        targets[row.session_id] = {
            "session_id": row.session_id,
            "runtime_agent_id": row.runtime_agent_id,
        }
    for row in inventory.ephemerals:
        if row.session_id:
            targets[row.session_id] = {
                "session_id": row.session_id,
                "runtime_agent_id": row.runtime_agent_id,
            }
    for row in inventory.creations:
        if row.session_id:
            targets[row.session_id] = {
                "session_id": row.session_id,
                "runtime_agent_id": row.runtime_agent_id,
            }
    return [targets[key] for key in sorted(targets)]


class RuntimeResourceStoreMixin:
    Session: sessionmaker

    def get_agent_version(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        digest: str,
    ) -> RuntimeAgentVersionModel | None:
        with self.Session() as db:
            row = db.get(RuntimeAgentVersionModel, (agent_id, agent_version_id, digest))
            return _detached(db, row)

    def agent_versions_for_agent(self, agent_id: str) -> list[RuntimeAgentVersionModel]:
        with self.Session() as db:
            rows = db.scalars(
                select(RuntimeAgentVersionModel).where(RuntimeAgentVersionModel.governance_agent_id == agent_id).order_by(RuntimeAgentVersionModel.created_at),
            ).all()
            return [_detached(db, row) for row in rows]

    def agent_deletion_pending(self, agent_id: str) -> bool:
        with self.Session() as db:
            return (
                db.scalar(
                    select(RuntimeAgentDeletionIntentModel.intent_id)
                    .where(
                        RuntimeAgentDeletionIntentModel.agent_id == agent_id,
                        RuntimeAgentDeletionIntentModel.status == "cleanup_pending",
                    )
                    .limit(1),
                )
                is not None
            )

    def start_ephemeral_resource(
        self,
        *,
        cache_key: str,
        business_agent_id: str,
        version_owner_id: str,
        agent_version_id: str,
        digest: str,
        source_id: str,
        source_kind: str,
        workspace_id: str,
    ) -> RuntimeEphemeralResourceModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            pending_deletion = db.scalar(
                select(RuntimeAgentDeletionIntentModel.intent_id)
                .where(
                    RuntimeAgentDeletionIntentModel.agent_id == business_agent_id,
                    RuntimeAgentDeletionIntentModel.status == "cleanup_pending",
                )
                .limit(1),
            )
            if pending_deletion:
                raise RuntimeStateConflict("Business Agent deletion is pending")
            row = db.get(RuntimeEphemeralResourceModel, cache_key)
            identity = (
                business_agent_id,
                version_owner_id,
                agent_version_id,
                digest,
                source_id,
                source_kind,
                workspace_id,
            )
            if row is not None and row.status != "cleanup_complete":
                actual = (
                    row.business_agent_id,
                    row.version_owner_id,
                    row.agent_version_id,
                    row.harness_digest,
                    row.source_id,
                    row.source_kind,
                    row.workspace_id,
                )
                if actual != identity:
                    raise RuntimeStateConflict("Ephemeral cache key is bound to another immutable resource")
                return _detached(db, row)
            now = utc_now()
            if row is None:
                row = RuntimeEphemeralResourceModel(
                    cache_key=cache_key,
                    business_agent_id=business_agent_id,
                    version_owner_id=version_owner_id,
                    agent_version_id=agent_version_id,
                    harness_digest=digest,
                    source_id=source_id,
                    source_kind=source_kind,
                    workspace_id=workspace_id,
                    status="provisioning",
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            else:
                row.business_agent_id = business_agent_id
                row.version_owner_id = version_owner_id
                row.agent_version_id = agent_version_id
                row.harness_digest = digest
                row.source_id = source_id
                row.source_kind = source_kind
                row.workspace_id = workspace_id
                row.runtime_agent_id = None
                row.session_id = None
                row.status = "provisioning"
                row.error_json = None
                row.created_at = now
                row.updated_at = now
                row.completed_at = None
            db.flush()
            return _detached(db, row)

    def get_ephemeral_resource(self, cache_key: str) -> RuntimeEphemeralResourceModel | None:
        with self.Session() as db:
            return _detached(db, db.get(RuntimeEphemeralResourceModel, cache_key))

    def record_ephemeral_agent(self, cache_key: str, runtime_agent_id: str) -> RuntimeEphemeralResourceModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_ephemeral_resource(db, cache_key)
            if row.runtime_agent_id not in {None, runtime_agent_id}:
                raise RuntimeStateConflict("Ephemeral resource returned another Runtime Agent")
            row.runtime_agent_id = runtime_agent_id
            row.error_json = None
            row.updated_at = utc_now()
            db.flush()
            return _detached(db, row)

    def record_ephemeral_session(self, cache_key: str, session_id: str) -> RuntimeEphemeralResourceModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_ephemeral_resource(db, cache_key)
            if row.session_id not in {None, session_id}:
                raise RuntimeStateConflict("Ephemeral resource returned another Runtime Session")
            row.session_id = session_id
            row.error_json = None
            row.updated_at = utc_now()
            db.flush()
            return _detached(db, row)

    def mark_ephemeral_ready(self, cache_key: str) -> RuntimeEphemeralResourceModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_ephemeral_resource(db, cache_key)
            if not row.runtime_agent_id or not row.session_id:
                raise RuntimeStateConflict("Ephemeral resource cannot become ready without Agent and Session")
            row.status = "ready"
            row.error_json = None
            row.updated_at = utc_now()
            db.flush()
            return _detached(db, row)

    def mark_ephemeral_cleanup_pending(
        self,
        cache_key: str,
        *,
        stage: str,
        error_type: str,
    ) -> RuntimeEphemeralResourceModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_ephemeral_resource(db, cache_key)
            row.status = "cleanup_pending"
            row.error_json = {"stage": stage, "error_type": error_type}
            row.updated_at = utc_now()
            db.flush()
            return _detached(db, row)

    def mark_ephemeral_awaiting_restart(
        self,
        cache_key: str,
        *,
        stage: str,
        error_type: str,
    ) -> RuntimeEphemeralResourceModel:
        """保留快照/定位符，等待 Runtime 启动时装载新 subagent 模板。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_ephemeral_resource(db, cache_key)
            row.status = "awaiting_restart"
            row.error_json = {"stage": stage, "error_type": error_type}
            row.updated_at = utc_now()
            db.flush()
            return _detached(db, row)

    def complete_ephemeral_resource(self, cache_key: str) -> RuntimeEphemeralResourceModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_ephemeral_resource(db, cache_key)
            if row.runtime_agent_id:
                binding = db.scalar(
                    select(RuntimeSessionBindingModel.session_id).where(RuntimeSessionBindingModel.runtime_agent_id == row.runtime_agent_id).limit(1),
                )
                version = db.scalar(
                    select(RuntimeAgentVersionModel.runtime_agent_id).where(RuntimeAgentVersionModel.runtime_agent_id == row.runtime_agent_id).limit(1),
                )
                if binding or version:
                    raise RuntimeStateConflict("Ephemeral local Runtime bindings still exist")
            now = utc_now()
            row.status = "cleanup_complete"
            row.error_json = None
            row.updated_at = now
            row.completed_at = now
            db.flush()
            return _detached(db, row)

    def recoverable_ephemeral_resources(
        self,
        *,
        include_ready: bool,
        awaiting_restart_before: str | None = None,
    ) -> list[RuntimeEphemeralResourceModel]:
        statuses = ["provisioning", "cleanup_pending"]
        if include_ready:
            statuses.extend(("ready", "awaiting_restart"))
        recoverable = RuntimeEphemeralResourceModel.status.in_(statuses)
        if awaiting_restart_before is not None:
            recoverable = or_(
                recoverable,
                and_(
                    RuntimeEphemeralResourceModel.status == "awaiting_restart",
                    RuntimeEphemeralResourceModel.updated_at <= awaiting_restart_before,
                ),
            )
        with self.Session() as db:
            rows = db.scalars(
                select(RuntimeEphemeralResourceModel).where(recoverable).order_by(RuntimeEphemeralResourceModel.created_at),
            ).all()
            return [_detached(db, row) for row in rows]

    def complete_ephemeral_resources_for_runtime_agents(self, runtime_agent_ids: set[str]) -> None:
        if not runtime_agent_ids:
            return
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            rows = db.scalars(
                select(RuntimeEphemeralResourceModel).where(
                    RuntimeEphemeralResourceModel.runtime_agent_id.in_(runtime_agent_ids),
                    RuntimeEphemeralResourceModel.status != "cleanup_complete",
                ),
            ).all()
            now = utc_now()
            for row in rows:
                row.status = "cleanup_complete"
                row.error_json = None
                row.updated_at = now
                row.completed_at = now

    def start_agent_deletion(
        self,
        *,
        agent_id: str,
        agent_generation: str,
        workspace_dir: str,
    ) -> RuntimeAgentDeletionIntentModel:
        """先保存完整定位信息，再允许调用方 tombstone 或删除远端资源。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            existing = db.scalar(
                select(RuntimeAgentDeletionIntentModel).where(
                    RuntimeAgentDeletionIntentModel.agent_id == agent_id,
                    RuntimeAgentDeletionIntentModel.status == "cleanup_pending",
                ),
            )
            if existing is not None:
                if existing.agent_generation != agent_generation:
                    raise RuntimeStateConflict("A previous Agent generation still has pending Runtime cleanup")
                return _detached(db, existing)

            active_run = db.scalar(
                select(AgentRunModel.run_id)
                .where(
                    AgentRunModel.agent_id == agent_id,
                    AgentRunModel.status.in_([status.value for status in ACTIVE_RUN_STATUSES]),
                )
                .limit(1),
            )
            if active_run:
                raise RuntimeStateConflict("Agent has an active run and cannot enter deletion cleanup")

            inventory = _load_deletion_inventory(db, agent_id)
            if any(row.runtime_agent_id is None for row in inventory.ephemerals):
                # 一个尚未落下远端 Agent 定位符的 create 可能仍在飞行。先让
                # provision/compensation 收敛，不能用不完整 intent 猜测删除对象。
                raise RuntimeStateConflict("Ephemeral Runtime provisioning must settle before Agent deletion")

            now = utc_now()
            intent = RuntimeAgentDeletionIntentModel(
                intent_id=f"agent-delete-{uuid.uuid4()}",
                agent_id=agent_id,
                agent_generation=agent_generation,
                workspace_dir=workspace_dir,
                versions_json=_deletion_version_targets(inventory, agent_id),
                sessions_json=_deletion_session_targets(inventory),
                created_at=now,
                updated_at=now,
            )
            db.add(intent)
            db.flush()
            return _detached(db, intent)

    def get_agent_deletion(self, intent_id: str) -> RuntimeAgentDeletionIntentModel:
        with self.Session() as db:
            row = db.get(RuntimeAgentDeletionIntentModel, intent_id)
            if row is None:
                raise RuntimeObjectNotFound(f"Agent deletion intent not found: {intent_id}")
            return _detached(db, row)

    def recoverable_agent_deletions(self) -> list[RuntimeAgentDeletionIntentModel]:
        with self.Session() as db:
            rows = db.scalars(
                select(RuntimeAgentDeletionIntentModel)
                .where(RuntimeAgentDeletionIntentModel.status == "cleanup_pending")
                .order_by(RuntimeAgentDeletionIntentModel.created_at),
            ).all()
            return [_detached(db, row) for row in rows]

    def record_agent_deletion_enumeration(
        self,
        intent_id: str,
        *,
        runtime_agent_id: str,
        session_ids: list[str],
    ) -> RuntimeAgentDeletionIntentModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_agent_deletion(db, intent_id)
            allowed_agents = {str(item.get("runtime_agent_id")) for item in row.versions_json or []}
            if runtime_agent_id not in allowed_agents:
                raise RuntimeStateConflict("Deletion enumeration returned an unowned Runtime Agent")
            sessions = {str(item.get("session_id")): dict(item) for item in row.sessions_json or [] if isinstance(item, dict) and item.get("session_id")}
            for session_id in session_ids:
                if not session_id:
                    raise RuntimeStateConflict("Runtime returned an invalid Session id during deletion")
                existing = sessions.get(session_id)
                if existing is not None and existing.get("runtime_agent_id") != runtime_agent_id:
                    raise RuntimeStateConflict("Runtime Session changed ownership during deletion")
                sessions[session_id] = {
                    "session_id": session_id,
                    "runtime_agent_id": runtime_agent_id,
                }
            enumerated = set(row.enumerated_runtime_agent_ids_json or [])
            enumerated.add(runtime_agent_id)
            row.sessions_json = [sessions[key] for key in sorted(sessions)]
            row.enumerated_runtime_agent_ids_json = sorted(enumerated)
            row.updated_at = utc_now()
            row.error_json = None
            db.flush()
            return _detached(db, row)

    def record_agent_deletion_progress(
        self,
        intent_id: str,
        *,
        deleted_session_id: str | None = None,
        deleted_runtime_agent_id: str | None = None,
        removed_snapshot_id: str | None = None,
        tombstoned: bool | None = None,
        workspace_removed: bool | None = None,
        error: JsonObject | None = None,
        increment_attempt: bool = False,
    ) -> RuntimeAgentDeletionIntentModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_agent_deletion(db, intent_id)
            if deleted_session_id:
                row.deleted_session_ids_json = sorted(
                    {*list(row.deleted_session_ids_json or []), deleted_session_id},
                )
            if deleted_runtime_agent_id:
                row.deleted_runtime_agent_ids_json = sorted(
                    {*list(row.deleted_runtime_agent_ids_json or []), deleted_runtime_agent_id},
                )
            if removed_snapshot_id:
                row.removed_snapshot_ids_json = sorted(
                    {*list(row.removed_snapshot_ids_json or []), removed_snapshot_id},
                )
            if tombstoned is not None:
                row.tombstoned = tombstoned
            if workspace_removed is not None:
                row.workspace_removed = workspace_removed
            if increment_attempt:
                row.attempts += 1
            row.error_json = dict(error) if error is not None else None
            row.updated_at = utc_now()
            db.flush()
            return _detached(db, row)

    def remove_agent_deletion_local_bindings(self, intent_id: str) -> None:
        """远端删除确认后，按 intent 定位并移除可重建的本地运行绑定。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_agent_deletion(db, intent_id)
            expected_sessions = {str(item.get("session_id")) for item in row.sessions_json or [] if isinstance(item, dict) and item.get("session_id")}
            expected_agents = {str(item.get("runtime_agent_id")) for item in row.versions_json or [] if isinstance(item, dict) and item.get("runtime_agent_id")}
            if not expected_sessions.issubset(set(row.deleted_session_ids_json or [])):
                raise RuntimeStateConflict("Runtime Sessions must be deleted before local binding cleanup")
            if not expected_agents.issubset(set(row.deleted_runtime_agent_ids_json or [])):
                raise RuntimeStateConflict("Runtime Agents must be deleted before local binding cleanup")
            bindings = db.scalars(
                select(RuntimeSessionBindingModel).where(RuntimeSessionBindingModel.agent_id == row.agent_id),
            ).all()
            if any(binding.active_run_id for binding in bindings):
                raise RuntimeStateConflict("An active run must terminate before local deletion cleanup")
            for binding in bindings:
                db.delete(binding)
            for creation in db.scalars(
                select(RuntimeSessionCreationIntentModel).where(RuntimeSessionCreationIntentModel.agent_id == row.agent_id),
            ).all():
                db.delete(creation)
            for version in db.scalars(
                select(RuntimeAgentVersionModel).where(RuntimeAgentVersionModel.governance_agent_id == row.agent_id),
            ).all():
                db.delete(version)

    def complete_agent_deletion(self, intent_id: str) -> RuntimeAgentDeletionIntentModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _require_agent_deletion(db, intent_id)
            expected_agents = {str(item.get("runtime_agent_id")) for item in row.versions_json or [] if isinstance(item, dict) and item.get("runtime_agent_id")}
            expected_sessions = {str(item.get("session_id")) for item in row.sessions_json or [] if isinstance(item, dict) and item.get("session_id")}
            expected_snapshots = {_deletion_snapshot_id(item) for item in row.versions_json or [] if isinstance(item, dict)}
            local_session = db.scalar(
                select(RuntimeSessionBindingModel.session_id).where(RuntimeSessionBindingModel.agent_id == row.agent_id).limit(1),
            )
            local_version = db.scalar(
                select(RuntimeAgentVersionModel.runtime_agent_id).where(RuntimeAgentVersionModel.governance_agent_id == row.agent_id).limit(1),
            )
            local_creation = db.scalar(
                select(RuntimeSessionCreationIntentModel.intent_id).where(RuntimeSessionCreationIntentModel.agent_id == row.agent_id).limit(1),
            )
            local_ephemeral = db.scalar(
                select(RuntimeEphemeralResourceModel.cache_key)
                .where(
                    RuntimeEphemeralResourceModel.business_agent_id == row.agent_id,
                    RuntimeEphemeralResourceModel.status != "cleanup_complete",
                )
                .limit(1),
            )
            if (
                expected_agents != set(row.enumerated_runtime_agent_ids_json or [])
                or not expected_sessions.issubset(set(row.deleted_session_ids_json or []))
                or not expected_agents.issubset(set(row.deleted_runtime_agent_ids_json or []))
                or not expected_snapshots.issubset(set(row.removed_snapshot_ids_json or []))
                or not row.tombstoned
                or not row.workspace_removed
                or local_session
                or local_version
                or local_creation
                or local_ephemeral
            ):
                raise RuntimeStateConflict("Agent deletion cleanup is not fully confirmed")
            now = utc_now()
            row.status = "cleanup_complete"
            row.error_json = None
            row.updated_at = now
            row.completed_at = now
            db.flush()
            return _detached(db, row)

    def get_agent_version_by_runtime_id(self, runtime_agent_id: str) -> RuntimeAgentVersionModel | None:
        with self.Session() as db:
            row = db.scalar(
                select(RuntimeAgentVersionModel).where(
                    RuntimeAgentVersionModel.runtime_agent_id == runtime_agent_id,
                ),
            )
            return _detached(db, row)

    def bind_agent_version(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        digest: str,
        runtime_agent_id: str,
        governance_agent_id: str | None = None,
        source_kind: str = "published",
        source_id: str | None = None,
    ) -> RuntimeAgentVersionModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            governed_id = governance_agent_id or agent_id
            pending_deletion = db.scalar(
                select(RuntimeAgentDeletionIntentModel.intent_id)
                .where(
                    RuntimeAgentDeletionIntentModel.agent_id == governed_id,
                    RuntimeAgentDeletionIntentModel.status == "cleanup_pending",
                )
                .limit(1),
            )
            if pending_deletion:
                raise RuntimeStateConflict("Business Agent deletion is pending")
            key = (agent_id, agent_version_id, digest)
            existing = db.get(RuntimeAgentVersionModel, key)
            if existing is not None:
                if (
                    existing.runtime_agent_id != runtime_agent_id
                    or existing.governance_agent_id != governed_id
                    or existing.source_kind != source_kind
                    or existing.source_id != source_id
                ):
                    raise RuntimeStateConflict("Agent version is already bound to another Runtime Agent")
                return _detached(db, existing)
            row = RuntimeAgentVersionModel(
                agent_id=agent_id,
                agent_version_id=agent_version_id,
                harness_digest=digest,
                runtime_agent_id=runtime_agent_id,
                governance_agent_id=governed_id,
                source_kind=source_kind,
                source_id=source_id,
            )
            db.add(row)
            db.flush()
            return _detached(db, row)

    def delete_agent_version(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        digest: str,
    ) -> bool:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.get(RuntimeAgentVersionModel, (agent_id, agent_version_id, digest))
            if row is None:
                return False
            bound = db.scalar(select(RuntimeSessionBindingModel.session_id).where(RuntimeSessionBindingModel.runtime_agent_id == row.runtime_agent_id).limit(1))
            if bound:
                raise RuntimeStateConflict("Runtime Agent still owns a Session binding")
            db.delete(row)
            return True
