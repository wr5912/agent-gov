from __future__ import annotations

import uuid

from agentgov_agentscope_contract import session_creation_token, session_workspace_id
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from ._store_support import (
    RuntimeObjectNotFound,
    RuntimeStateConflict,
    SessionCreationStatus,
    _append_json_id,
    _base_workspace_id,
    _detached,
    _require_run,
    _require_session_creation,
    _runtime_context_response,
    _version_identity,
)
from .contracts import (
    ACTIVE_RUN_STATUSES,
    RunStatus,
    RuntimeChildSessionRegistration,
    RuntimeContextResponse,
    RuntimeTeamInboxAck,
    RuntimeTeamInboxDelivery,
)
from .models import (
    RuntimeAgentDeletionIntentModel,
    RuntimeSessionBindingModel,
    RuntimeSessionCreationIntentModel,
    RuntimeTeamDeliveryModel,
)
from .operation_identity import (
    LEGACY_UNKNOWN_SESSION_REQUEST_FINGERPRINT,
    session_creation_request_fingerprint,
)


def _run_topology_is_frozen(metadata: JsonObject | None) -> bool:
    values = metadata or {}
    return values.get("cancellation_requested") is True or values.get("recovery_required") is True


def _validate_session_creation_request(
    intent: RuntimeSessionCreationIntentModel,
    *,
    runtime_agent_id: str,
    requested_name: str | None,
) -> None:
    if intent.runtime_agent_id != runtime_agent_id:
        raise RuntimeStateConflict(
            "Idempotency-Key is already bound to another Runtime Agent",
        )
    if intent.request_fingerprint == LEGACY_UNKNOWN_SESSION_REQUEST_FINGERPRINT:
        raise RuntimeStateConflict(
            "Legacy Session creation intent has no replay-safe request identity",
        )
    requested_fingerprint = session_creation_request_fingerprint(
        runtime_agent_id,
        requested_name,
    )
    if intent.request_fingerprint != requested_fingerprint:
        raise RuntimeStateConflict(
            "Idempotency-Key is already bound to another immutable Session request",
        )


class RuntimeSessionStoreMixin:
    Session: sessionmaker

    def session_creation_for_key(self, key: str) -> RuntimeSessionCreationIntentModel | None:
        with self.Session() as db:
            row = db.scalar(select(RuntimeSessionCreationIntentModel).where(RuntimeSessionCreationIntentModel.idempotency_key == key))
            return _detached(db, row)

    def session_creation_for_request(
        self,
        *,
        key: str,
        runtime_agent_id: str,
        requested_name: str | None,
    ) -> RuntimeSessionCreationIntentModel | None:
        """Read an idempotent intent only after validating its full public request."""

        with self.Session() as db:
            row = db.scalar(
                select(RuntimeSessionCreationIntentModel).where(
                    RuntimeSessionCreationIntentModel.idempotency_key == key,
                ),
            )
            if row is None:
                return None
            _validate_session_creation_request(
                row,
                runtime_agent_id=runtime_agent_id,
                requested_name=requested_name,
            )
            return _detached(db, row)

    def start_session_creation(
        self,
        *,
        idempotency_key: str | None,
        agent_id: str,
        agent_version_id: str,
        runtime_agent_id: str,
        digest: str,
        workspace_id: str,
        requested_name: str | None,
    ) -> tuple[RuntimeSessionCreationIntentModel, bool]:
        """原子占有一个创建 intent；同 key 只有首次调用获得执行权。"""

        request_fingerprint = session_creation_request_fingerprint(
            runtime_agent_id,
            requested_name,
        )
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            existing = (
                db.scalar(select(RuntimeSessionCreationIntentModel).where(RuntimeSessionCreationIntentModel.idempotency_key == idempotency_key))
                if idempotency_key
                else None
            )
            if existing is not None:
                _validate_session_creation_request(
                    existing,
                    runtime_agent_id=runtime_agent_id,
                    requested_name=requested_name,
                )
                if (
                    _version_identity(existing) != (agent_id, agent_version_id, runtime_agent_id, digest)
                    or _base_workspace_id(existing.workspace_id) != workspace_id
                ):
                    raise RuntimeStateConflict("Idempotency-Key is already bound to another Agent version")
                return _detached(db, existing), False
            now = utc_now()
            identity = uuid.uuid4()
            intent_id = session_creation_token(identity)
            intent = RuntimeSessionCreationIntentModel(
                intent_id=intent_id,
                idempotency_key=idempotency_key,
                agent_id=agent_id,
                agent_version_id=agent_version_id,
                runtime_agent_id=runtime_agent_id,
                harness_digest=digest,
                workspace_id=session_workspace_id(workspace_id, identity),
                request_fingerprint=request_fingerprint,
                status=SessionCreationStatus.PENDING.value,
                created_at=now,
                updated_at=now,
            )
            db.add(intent)
            db.flush()
            return _detached(db, intent), True

    def bind_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        agent_version_id: str,
        runtime_agent_id: str,
        digest: str,
        idempotency_key: None = None,
    ) -> RuntimeSessionBindingModel:
        """绑定非 API 创建流的 Session；Idempotency-Key 只归 intent 表所有。"""

        del idempotency_key
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            pending_deletion = db.scalar(
                select(RuntimeAgentDeletionIntentModel.intent_id)
                .where(
                    RuntimeAgentDeletionIntentModel.agent_id == agent_id,
                    RuntimeAgentDeletionIntentModel.status == "cleanup_pending",
                )
                .limit(1),
            )
            if pending_deletion:
                raise RuntimeStateConflict("Business Agent deletion is pending")
            existing = db.get(RuntimeSessionBindingModel, session_id)
            if existing is not None:
                if _version_identity(existing) != (agent_id, agent_version_id, runtime_agent_id, digest):
                    raise RuntimeStateConflict("Runtime Session is already bound to another Agent version")
                return _detached(db, existing)
            row = RuntimeSessionBindingModel(
                session_id=session_id,
                agent_id=agent_id,
                agent_version_id=agent_version_id,
                runtime_agent_id=runtime_agent_id,
                harness_digest=digest,
                root_session_id=session_id,
            )
            db.add(row)
            db.flush()
            return _detached(db, row)

    def bind_team_child(
        self,
        registration: RuntimeChildSessionRegistration,
    ) -> RuntimeContextResponse:
        """把 AgentScope 动态 worker Session 绑定到当前顶层 run。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = _require_run(db, registration.run_id)
            if RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                raise RuntimeStateConflict("Team child can only bind to an active run")
            if run.session_id != registration.parent_session_id:
                raise RuntimeStateConflict("Team child parent does not match the run root Session")
            if registration.child_session_id == registration.parent_session_id:
                raise RuntimeStateConflict("Team child Session must differ from its parent")
            parent = db.get(RuntimeSessionBindingModel, registration.parent_session_id)
            if (
                parent is None
                or parent.active_run_id != run.run_id
                or parent.root_session_id != run.session_id
                or parent.runtime_agent_id != run.runtime_agent_id
            ):
                raise RuntimeStateConflict("Team child parent does not own the active run fence")

            child = db.get(RuntimeSessionBindingModel, registration.child_session_id)
            if child is None:
                if _run_topology_is_frozen(run.metadata_json):
                    raise RuntimeStateConflict(
                        "Team child cannot bind after cancellation or recovery started",
                    )
                child = RuntimeSessionBindingModel(
                    session_id=registration.child_session_id,
                    agent_id=run.agent_id,
                    agent_version_id=run.agent_version_id,
                    runtime_agent_id=registration.child_runtime_agent_id,
                    harness_digest=run.harness_digest,
                    root_session_id=run.session_id,
                    team_id=registration.team_id,
                    active_run_id=run.run_id,
                )
                db.add(child)
            else:
                expected = (
                    run.agent_id,
                    run.agent_version_id,
                    registration.child_runtime_agent_id,
                    run.harness_digest,
                    run.session_id,
                    registration.team_id,
                )
                actual = (
                    child.agent_id,
                    child.agent_version_id,
                    child.runtime_agent_id,
                    child.harness_digest,
                    child.root_session_id,
                    child.team_id,
                )
                if actual != expected:
                    raise RuntimeStateConflict("Team child Session is already bound to another governed identity")
                if child.active_run_id not in {None, run.run_id}:
                    raise RuntimeStateConflict("Team child Session belongs to another active run")
                if child.active_run_id != run.run_id and _run_topology_is_frozen(
                    run.metadata_json,
                ):
                    raise RuntimeStateConflict(
                        "Team child cannot rebind after cancellation or recovery started",
                    )
                child.active_run_id = run.run_id
            parent.team_id = registration.team_id
            now = utc_now()
            child.updated_at = now
            parent.updated_at = now
            db.flush()
            return _runtime_context_response(run, child)

    def record_team_inbox_delivery(
        self,
        delivery: RuntimeTeamInboxDelivery,
    ) -> RuntimeTeamInboxAck:
        """在同一写事务内去重 delivery 并分配严格递增 generation。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            existing = db.get(RuntimeTeamDeliveryModel, delivery.event_id)
            if existing is not None:
                if (
                    existing.run_id,
                    existing.source_session_id,
                    existing.target_session_id,
                ) != (
                    delivery.run_id,
                    delivery.source_session_id,
                    delivery.target_session_id,
                ):
                    raise RuntimeStateConflict("Team delivery event_id was reused with another payload")
                return RuntimeTeamInboxAck(
                    run_id=existing.run_id,
                    event_id=existing.event_id,
                    generation=existing.generation,
                )

            run = _require_run(db, delivery.run_id)
            if RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
                raise RuntimeStateConflict("Team delivery requires an active run")
            if _run_topology_is_frozen(run.metadata_json):
                raise RuntimeStateConflict(
                    "Team delivery cannot expand a run after cancellation or recovery started",
                )
            if delivery.source_session_id == delivery.target_session_id:
                raise RuntimeStateConflict("Team delivery must cross Session boundaries")
            source = db.get(RuntimeSessionBindingModel, delivery.source_session_id)
            target = db.get(RuntimeSessionBindingModel, delivery.target_session_id)
            if source is None or source.active_run_id != run.run_id:
                raise RuntimeStateConflict("Team delivery source does not own the run fence")
            if target is None or target.root_session_id != run.session_id:
                raise RuntimeStateConflict("Team delivery target is not bound to the run root")
            if target.session_id == run.session_id:
                if source.session_id == run.session_id or source.team_id is None:
                    raise RuntimeStateConflict("Only a bound Team worker can deliver to the root")
            else:
                if target.team_id is None or source.team_id != target.team_id:
                    raise RuntimeStateConflict("Team delivery cannot cross governed Team membership")
                if target.active_run_id not in {None, run.run_id}:
                    raise RuntimeStateConflict("Team delivery target belongs to another active run")
                target.active_run_id = run.run_id

            generation = run.team_generation + 1
            run.team_generation = generation
            if target.session_id != run.session_id:
                target.active_team_generation = generation
                _append_json_id(run, "pending_child_session_ids_json", target.session_id)
            row = RuntimeTeamDeliveryModel(
                event_id=delivery.event_id,
                run_id=run.run_id,
                source_session_id=source.session_id,
                target_session_id=target.session_id,
                generation=generation,
            )
            db.add(row)
            now = utc_now()
            run.updated_at = now
            target.updated_at = now
            db.flush()
            return RuntimeTeamInboxAck(
                run_id=run.run_id,
                event_id=row.event_id,
                generation=generation,
            )

    def record_session_creation_upstream(self, intent_id: str, session_id: str) -> RuntimeSessionCreationIntentModel:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            intent = _require_session_creation(db, intent_id)
            if intent.session_id not in {None, session_id}:
                raise RuntimeStateConflict("Session creation intent returned a different Runtime session")
            if SessionCreationStatus(intent.status) not in {
                SessionCreationStatus.PENDING,
                SessionCreationStatus.UPSTREAM_CREATED,
                SessionCreationStatus.CLEANUP_PENDING,
            }:
                raise RuntimeStateConflict("Session creation intent is already terminal")
            intent.session_id = session_id
            intent.status = SessionCreationStatus.UPSTREAM_CREATED.value
            intent.updated_at = utc_now()
            db.flush()
            return _detached(db, intent)

    def complete_session_creation(self, intent_id: str) -> RuntimeSessionBindingModel:
        """在一个本地事务内创建 binding 并把 intent 标为 bound。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            intent = _require_session_creation(db, intent_id)
            if not intent.session_id:
                raise RuntimeStateConflict("Session creation intent has no Runtime session")
            binding = db.get(RuntimeSessionBindingModel, intent.session_id)
            if binding is None:
                binding = RuntimeSessionBindingModel(
                    session_id=intent.session_id,
                    agent_id=intent.agent_id,
                    agent_version_id=intent.agent_version_id,
                    runtime_agent_id=intent.runtime_agent_id,
                    harness_digest=intent.harness_digest,
                    root_session_id=intent.session_id,
                )
                db.add(binding)
                db.flush()
            elif _version_identity(binding) != _version_identity(intent):
                raise RuntimeStateConflict("Runtime Session is already bound to another Agent version")
            now = utc_now()
            intent.status = SessionCreationStatus.BOUND.value
            intent.error_json = None
            intent.updated_at = now
            intent.completed_at = now
            db.flush()
            return _detached(db, binding)

    def mark_session_creation(
        self,
        intent_id: str,
        *,
        status: SessionCreationStatus,
        error: JsonObject,
        cleanup_attempt: bool = True,
    ) -> RuntimeSessionCreationIntentModel:
        if status not in {
            SessionCreationStatus.PENDING,
            SessionCreationStatus.CLEANUP_PENDING,
            SessionCreationStatus.FAILED,
            SessionCreationStatus.FAILED_CLEANED,
        }:
            raise ValueError("Unsupported Session creation failure status")
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            intent = _require_session_creation(db, intent_id)
            now = utc_now()
            intent.status = status.value
            intent.error_json = dict(error)
            intent.updated_at = now
            if status is SessionCreationStatus.CLEANUP_PENDING and cleanup_attempt:
                intent.cleanup_attempts += 1
            if status in {SessionCreationStatus.FAILED, SessionCreationStatus.FAILED_CLEANED}:
                intent.completed_at = now
            db.flush()
            return _detached(db, intent)

    def recoverable_session_creations(self, *, updated_before: str | None) -> list[RuntimeSessionCreationIntentModel]:
        statuses = [
            SessionCreationStatus.PENDING.value,
            SessionCreationStatus.UPSTREAM_CREATED.value,
            SessionCreationStatus.CLEANUP_PENDING.value,
        ]
        with self.Session() as db:
            query = select(RuntimeSessionCreationIntentModel).where(
                RuntimeSessionCreationIntentModel.status.in_(statuses),
            )
            if updated_before is not None:
                query = query.where(RuntimeSessionCreationIntentModel.updated_at <= updated_before)
            rows = db.scalars(query.order_by(RuntimeSessionCreationIntentModel.created_at)).all()
            return [_detached(db, row) for row in rows]

    def get_session(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        runtime_agent_id: str | None = None,
    ) -> RuntimeSessionBindingModel:
        with self.Session() as db:
            row = db.get(RuntimeSessionBindingModel, session_id)
            if row is None:
                raise RuntimeObjectNotFound(f"Runtime session not found: {session_id}")
            if agent_id is not None and row.agent_id != agent_id:
                raise RuntimeObjectNotFound(f"Runtime session not found: {session_id}")
            if runtime_agent_id is not None and row.runtime_agent_id != runtime_agent_id:
                raise RuntimeObjectNotFound(f"Runtime session not found: {session_id}")
            return _detached(db, row)

    def sessions_for_agent(self, agent_id: str) -> list[RuntimeSessionBindingModel]:
        with self.Session() as db:
            rows = db.scalars(
                select(RuntimeSessionBindingModel).where(RuntimeSessionBindingModel.agent_id == agent_id).order_by(RuntimeSessionBindingModel.created_at.desc())
            ).all()
            return [_detached(db, row) for row in rows]

    def sessions_for_runtime_agent(self, runtime_agent_id: str) -> list[RuntimeSessionBindingModel]:
        with self.Session() as db:
            rows = db.scalars(
                select(RuntimeSessionBindingModel)
                .where(RuntimeSessionBindingModel.runtime_agent_id == runtime_agent_id)
                .order_by(RuntimeSessionBindingModel.created_at.desc()),
            ).all()
            return [_detached(db, row) for row in rows]

    def delete_session_binding(self, session_id: str) -> bool:
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.get(RuntimeSessionBindingModel, session_id)
            if row is None:
                return False
            if row.active_run_id:
                raise RuntimeStateConflict("An active run must terminate before the session can be deleted")
            db.delete(row)
            return True
