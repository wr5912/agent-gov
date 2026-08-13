from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..advisory_lock import advisory_lock
from ..agent_paths import (
    BusinessAgentLayout,
    InvalidAgentId,
    business_agent_layout,
    business_agent_repository_lock_path,
    validate_agent_id,
)
from ..agent_profiles import AgentRuntimeProfile, read_requires_web_hitl
from ..agent_registry_db import AgentRegistryModel
from ..business_agent_identity import business_agent_instance_etag
from ..business_agent_lifecycle import BusinessAgentMutationPrecondition, business_agent_mutation_precondition
from ..errors import BusinessRuleViolation, ConflictError, DataIntegrityError, NotFoundError
from ..runtime_db import utc_now
from ..runtime_db_base import begin_sqlite_write_transaction
from ..runtime_recovery import runtime_operation_heartbeat, runtime_operation_is_stale
from ..state_machines import validate_transition

_PROVISIONING = "provisioning"
_PROVISION_READY = "ready"
_NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


@dataclass(frozen=True)
class AgentRegistryRecord:
    """业务 Agent 的稳定身份记录（被治理对象的归属锚点）。"""

    agent_id: str
    name: str
    category: str
    workspace_dir: str
    created_at: str
    instance_etag: str
    status: str = "active"
    requires_web_hitl: bool = False  # 从 workspace project settings permissions.ask 派生的只读观测值


@dataclass(frozen=True)
class AgentProvisionReservation:
    """Opaque ownership claim for one DB + workspace provisioning saga."""

    agent_id: str
    token: str
    created_new: bool
    require_workspace_absent: bool = False


@dataclass(frozen=True)
class AgentProvisionOutcome:
    """Fresh durable outcome for one exact provisioning reservation."""

    state: Literal["completed", "owned", "indeterminate"]
    record: AgentRegistryRecord | None = None


@dataclass(frozen=True)
class _ProvisionRecoveryCandidate:
    """Read-only snapshot revalidated after taking the stable Agent lock."""

    agent_id: str
    token: str | None
    heartbeat: str | None
    workspace_dir: str


class AgentIdentityReservedError(ConflictError):
    """The stable Agent id already belongs to a live or permanently deleted identity."""


class _IncompleteWorkspaceRecovery(BaseModel):
    """Durable marker that prevents an unverified partial workspace from reuse."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["workspace_must_be_absent"] = "workspace_must_be_absent"
    workspace_dir: _NonEmptyText


class _AgentProvisionPrevious(BaseModel):
    """Typed rollback state; JSON exists only in the ORM persistence column."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: _NonEmptyText
    category: _NonEmptyText
    workspace_dir: _NonEmptyText
    created_at: _NonEmptyText
    status: _NonEmptyText
    deleted_at: _NonEmptyText
    provision_completed_token: str | None = None
    workspace_recovery: _IncompleteWorkspaceRecovery | None = None


class AgentRegistryStore:
    """业务 Agent 身份注册表存储（AGV-004/022 基座）。

    只登记业务 Agent（被治理对象）；治理 Agent（闭环执行者）不入注册表。
    `sync_business_agents` 幂等，可重复调用而不重复登记。
    """

    def __init__(self, session_factory: sessionmaker, *, data_dir: Path | None = None) -> None:
        self._session_factory = session_factory
        self._data_dir = data_dir

    def sync_business_agents(self, profiles: dict[str, AgentRuntimeProfile]) -> None:
        with self._session_factory.begin() as db:
            for profile in profiles.values():
                if profile.category != "business":
                    continue
                # 运行态 Workspace 的直接子目录名即业务 Agent 稳定身份。
                existing = db.get(AgentRegistryModel, profile.name)
                if existing is not None:
                    # Tombstones, including a never-public incomplete-workspace
                    # quarantine, are visibility fences.  They may intentionally
                    # have no completion token and disk discovery must not turn
                    # that absence into a startup failure or revive the row.
                    if existing.deleted_at:
                        continue
                    # 未完成创建是内部 saga intent；磁盘发现不得把它提前 finalize 或改写。
                    if (existing.provision_state or _PROVISION_READY) != _PROVISION_READY:
                        continue
                    _ready_token(existing)
                    # 已存在记录若 workspace_dir 漂移（升级后路径迁移）同步更新。
                    if existing.workspace_dir != str(profile.workspace_dir):
                        existing.workspace_dir = str(profile.workspace_dir)
                    continue
                db.add(
                    AgentRegistryModel(
                        agent_id=profile.name,
                        name=profile.name,
                        category=profile.category,
                        workspace_dir=str(profile.workspace_dir),
                        created_at=utc_now(),
                        provision_state=_PROVISION_READY,
                        provision_completed_token=uuid4().hex,
                    )
                )

    def list_agents(self) -> list[AgentRegistryRecord]:
        with self._session_factory.begin() as db:
            rows = (
                db.query(AgentRegistryModel)
                .filter(AgentRegistryModel.deleted_at.is_(None))  # #26：过滤 tombstone（已删除）
                .filter(AgentRegistryModel.provision_state == _PROVISION_READY)
                .order_by(AgentRegistryModel.created_at, AgentRegistryModel.agent_id)
                .all()
            )
            return [_record(row) for row in rows]

    def get_agent(self, agent_id: str) -> AgentRegistryRecord | None:
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            return _record(row) if row is not None and _is_public(row) else None

    def mutation_precondition(
        self,
        *,
        agent_id: str,
        expected_instance_etag: str,
        allow_workspace_activation: bool = False,
    ) -> BusinessAgentMutationPrecondition:
        """Build the shared read-only lifecycle fence for a stable-lock writer."""

        return business_agent_mutation_precondition(
            self._session_factory,
            agent_id=agent_id,
            expected_instance_etag=expected_instance_etag,
            allow_workspace_activation=allow_workspace_activation,
        )

    def create_business_agent(self, *, name: str, agent_id: str, workspace_dir: str) -> AgentRegistryRecord:
        """注册一个业务 Agent 身份（被治理对象）。活跃 agent_id 重复拒绝，空 name 拒绝。

        tombstone 永久保留；同 ID 只有在 generation 贯穿全部事实表后才可能重新开放。
        """
        safe_agent_id = validate_agent_id(agent_id)
        if safe_agent_id != agent_id:
            raise InvalidAgentId(f"Invalid agent_id: {agent_id!r}")
        agent_id = safe_agent_id
        clean_name = name.strip()
        if not clean_name:
            raise BusinessRuleViolation("Business agent name cannot be empty")
        created_at = utc_now()
        completed_token = uuid4().hex
        with self._session_factory.begin() as db:
            existing = db.get(AgentRegistryModel, agent_id)
            if existing is not None:
                raise AgentIdentityReservedError(f"Business agent id is already reserved: {agent_id}")
            db.add(
                AgentRegistryModel(
                    agent_id=agent_id,
                    name=clean_name,
                    category="business",
                    workspace_dir=workspace_dir,
                    created_at=created_at,
                    provision_state=_PROVISION_READY,
                    provision_completed_token=completed_token,
                )
            )
        return AgentRegistryRecord(
            agent_id=agent_id,
            name=clean_name,
            category="business",
            workspace_dir=workspace_dir,
            created_at=created_at,
            instance_etag=business_agent_instance_etag(completed_token),
            requires_web_hitl=read_requires_web_hitl(Path(workspace_dir)),
        )

    def reserve_business_agent(self, *, name: str, agent_id: str, workspace_dir: str) -> AgentProvisionReservation:
        """Persist an invisible, exclusive creation intent before touching the workspace."""
        safe_agent_id = validate_agent_id(agent_id)
        if safe_agent_id != agent_id:
            raise InvalidAgentId(f"Invalid agent_id: {agent_id!r}")
        agent_id = safe_agent_id
        clean_name = name.strip()
        if not clean_name:
            raise BusinessRuleViolation("Business agent name cannot be empty")
        recovery_layout = _canonical_recovery_layout(agent_id, workspace_dir)
        if recovery_layout is None:
            return self._reserve_business_agent(clean_name, agent_id, workspace_dir, recovery_layout=None)
        with advisory_lock(
            business_agent_repository_lock_path(recovery_layout.root.parents[1], agent_id),
            mode="exclusive",
        ):
            return self._reserve_business_agent(clean_name, agent_id, workspace_dir, recovery_layout=recovery_layout)

    def _reserve_business_agent(
        self,
        clean_name: str,
        agent_id: str,
        workspace_dir: str,
        *,
        recovery_layout: BusinessAgentLayout | None,
    ) -> AgentProvisionReservation:
        now = utc_now()
        token = uuid4().hex
        created_new = False
        require_workspace_absent = False
        with self._session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.get(AgentRegistryModel, agent_id)
            if row is not None and recovery_layout is not None and _is_never_public_quarantine(row, recovery_layout):
                if not _layout_root_is_absent(recovery_layout):
                    raise ConflictError(f"Incomplete Business agent cannot be reused safely until its entire layout root is absent: {agent_id}")
                db.delete(row)
                db.flush()
                row = None
                require_workspace_absent = True
            if row is None:
                if recovery_layout is not None and not _layout_root_is_absent(recovery_layout):
                    raise ConflictError(f"Business agent layout already contains unowned state: {agent_id}")
                created_new = True
                require_workspace_absent = recovery_layout is not None
                db.add(
                    AgentRegistryModel(
                        agent_id=agent_id,
                        name=clean_name,
                        category="business",
                        workspace_dir=workspace_dir,
                        created_at=now,
                        status="active",
                        provision_state=_PROVISIONING,
                        provision_token=token,
                        provision_completed_token=None,
                        provision_started_at=now,
                    )
                )
            else:
                raise AgentIdentityReservedError(f"Business agent id is already reserved: {agent_id}")
            db.flush()
        return AgentProvisionReservation(
            agent_id=agent_id,
            token=token,
            created_new=created_new,
            require_workspace_absent=require_workspace_absent,
        )

    def finalize_business_agent(
        self,
        reservation: AgentProvisionReservation,
        *,
        transaction_mutation: Callable[[Session], None] | None = None,
    ) -> AgentRegistryRecord:
        """Publish one reserved row only after its complete workspace is durable."""
        with self._session_factory() as db:
            db.begin()
            try:
                begin_sqlite_write_transaction(db.connection())
                row = _owned_reservation(db.get(AgentRegistryModel, reservation.agent_id), reservation)
                validate_transition("agent_provision", _PROVISIONING, _PROVISION_READY)
                row.deleted_at = None
                row.provision_state = _PROVISION_READY
                row.provision_completed_token = reservation.token
                row.provision_token = None
                row.provision_started_at = None
                row.provision_previous_json = None
                if transaction_mutation is not None:
                    transaction_mutation(db)
                db.flush()
                record = _record(row)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return record

    def resolve_business_agent_provision(self, reservation: AgentProvisionReservation) -> AgentProvisionOutcome:
        """Read the exact reservation outcome without opening a committing transaction."""
        with self._session_factory() as db:
            row = db.get(AgentRegistryModel, reservation.agent_id)
            if row is None:
                return AgentProvisionOutcome("indeterminate")
            if row.provision_state == _PROVISIONING and row.provision_token == reservation.token:
                return AgentProvisionOutcome("owned")
            if row.provision_state == _PROVISION_READY and row.provision_completed_token == reservation.token and _is_public(row):
                return AgentProvisionOutcome("completed", _record(row))
            return AgentProvisionOutcome("indeterminate")

    def compensate_business_agent(
        self,
        reservation: AgentProvisionReservation,
        *,
        workspace_cleanup_complete: bool,
    ) -> None:
        """Undo a failed reservation without exposing a partial Agent."""
        with self._session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _owned_reservation(db.get(AgentRegistryModel, reservation.agent_id), reservation)
            previous = row.provision_previous_json
            if previous is not None:
                attempted_workspace = row.workspace_dir
                _restore_snapshot(row, _parse_snapshot(previous))
                if not workspace_cleanup_complete:
                    _mark_incomplete_workspace(row, attempted_workspace)
            elif reservation.created_new and workspace_cleanup_complete:
                db.delete(row)
            else:
                # Unknown FS residue must retain a tombstone so startup disk discovery cannot revive it.
                _tombstone_incomplete(row)

    def renew_business_agent_provision(
        self,
        reservation: AgentProvisionReservation,
        *,
        now: str | None = None,
    ) -> None:
        """Renew the persisted saga lease after a durable filesystem step."""
        with self._session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _owned_reservation(db.get(AgentRegistryModel, reservation.agent_id), reservation)
            row.provision_started_at = runtime_operation_heartbeat(now=now)

    def recover_incomplete_provisions(self, *, now: str | None = None) -> int:
        """Fail closed under the same stable Agent lock used by creation.

        Candidate discovery is deliberately read-only.  Waiting for a filesystem
        lock while holding SQLite's write lock reverses the create path's
        lock order and can deadlock startup recovery against a live creator.
        """

        recovery_now = runtime_operation_heartbeat(now=now)
        with self._session_factory() as db:
            candidates = [
                _ProvisionRecoveryCandidate(
                    agent_id=str(row.agent_id),
                    token=row.provision_token,
                    heartbeat=row.provision_started_at,
                    workspace_dir=str(row.workspace_dir),
                )
                for row in db.scalars(
                    select(AgentRegistryModel).where(
                        AgentRegistryModel.provision_state == _PROVISIONING,
                    )
                ).all()
            ]

        recovered = 0
        for candidate in candidates:
            lock_path = self._provision_recovery_lock_path(candidate)
            if lock_path is None:
                continue
            with advisory_lock(lock_path, mode="exclusive"):
                recovered += int(self._recover_candidate(candidate, recovery_now=recovery_now))
        return recovered

    def _recover_candidate(
        self,
        candidate: _ProvisionRecoveryCandidate,
        *,
        recovery_now: str,
    ) -> bool:
        with self._session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.get(AgentRegistryModel, candidate.agent_id)
            if (
                row is None
                or row.provision_state != _PROVISIONING
                or row.provision_token != candidate.token
                or row.provision_started_at != candidate.heartbeat
                or not runtime_operation_is_stale(row.provision_started_at, now=recovery_now)
            ):
                return False
            previous = row.provision_previous_json
            attempted_workspace = row.workspace_dir
            try:
                if previous is not None:
                    _restore_snapshot(row, _parse_snapshot(previous))
                    _mark_incomplete_workspace(row, attempted_workspace)
                else:
                    _tombstone_incomplete(row)
            except DataIntegrityError:
                _tombstone_incomplete(row)
            return True

    def _provision_recovery_lock_path(
        self,
        candidate: _ProvisionRecoveryCandidate,
    ) -> Path | None:
        if self._data_dir is not None:
            try:
                return business_agent_repository_lock_path(self._data_dir, candidate.agent_id)
            except ValueError:
                return None
        layout = _canonical_recovery_layout(candidate.agent_id, candidate.workspace_dir)
        if layout is None:
            return None
        return business_agent_repository_lock_path(layout.root.parents[1], candidate.agent_id)

    def transition_business_agent(self, agent_id: str, *, status: str) -> AgentRegistryRecord:
        """业务 Agent 生命周期状态转移（AGV-020）。

        合法转移由 `agent_lifecycle` 状态机判定，非法转移抛 StateTransitionError（可理解错误）。
        main-agent 不再特判：它是可删除、可归档的普通业务 Agent。
        """
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            if row is None or not _is_public(row):
                raise NotFoundError(f"Business agent not found: {agent_id}")
            validate_transition("agent_lifecycle", row.status or "active", status)
            row.status = status
            return _record(row)


def _record(row: AgentRegistryModel) -> AgentRegistryRecord:
    return AgentRegistryRecord(
        agent_id=row.agent_id,
        name=row.name,
        category=row.category,
        workspace_dir=row.workspace_dir,
        created_at=row.created_at,
        instance_etag=business_agent_instance_etag(_ready_token(row)),
        status=row.status or "active",
        requires_web_hitl=read_requires_web_hitl(Path(row.workspace_dir)),
    )


def _is_public(row: AgentRegistryModel) -> bool:
    return not row.deleted_at and (row.provision_state or _PROVISION_READY) == _PROVISION_READY


def _ready_token(row: AgentRegistryModel) -> str:
    token = row.provision_completed_token
    if token:
        return token
    raise DataIntegrityError(f"Business agent ready instance token is missing: {row.agent_id}")


def _owned_reservation(
    row: AgentRegistryModel | None,
    reservation: AgentProvisionReservation,
) -> AgentRegistryModel:
    if row is None or row.provision_state != _PROVISIONING or row.provision_token != reservation.token:
        raise ConflictError(f"Business agent provisioning claim was lost: {reservation.agent_id}")
    return row


def _snapshot_row(row: AgentRegistryModel) -> _AgentProvisionPrevious:
    if not row.deleted_at:
        raise DataIntegrityError("Cannot snapshot a non-tombstoned Agent provisioning row")
    return _AgentProvisionPrevious(
        name=row.name,
        category=row.category,
        workspace_dir=row.workspace_dir,
        created_at=row.created_at,
        status=row.status,
        deleted_at=row.deleted_at,
        provision_completed_token=row.provision_completed_token,
        workspace_recovery=(_parse_workspace_recovery(row.provision_previous_json) if row.provision_previous_json is not None else None),
    )


def _parse_snapshot(value: object) -> _AgentProvisionPrevious:
    try:
        return _AgentProvisionPrevious.model_validate(value)
    except ValidationError as exc:
        raise DataIntegrityError("Invalid Agent provisioning recovery snapshot") from exc


def _parse_workspace_recovery(value: object) -> _IncompleteWorkspaceRecovery:
    try:
        return _IncompleteWorkspaceRecovery.model_validate(value)
    except ValidationError as exc:
        raise DataIntegrityError("Invalid incomplete Agent workspace recovery marker") from exc


def _restore_snapshot(row: AgentRegistryModel, snapshot: _AgentProvisionPrevious) -> None:
    validate_transition("agent_provision", row.provision_state, _PROVISION_READY)
    row.name = snapshot.name
    row.category = snapshot.category
    row.workspace_dir = snapshot.workspace_dir
    row.created_at = snapshot.created_at
    row.status = snapshot.status
    row.deleted_at = snapshot.deleted_at
    row.provision_state = _PROVISION_READY
    row.provision_token = None
    row.provision_completed_token = snapshot.provision_completed_token
    row.provision_started_at = None
    row.provision_previous_json = snapshot.workspace_recovery.model_dump(mode="json") if snapshot.workspace_recovery is not None else None


def _tombstone_incomplete(row: AgentRegistryModel) -> None:
    validate_transition("agent_provision", row.provision_state, _PROVISION_READY)
    row.deleted_at = row.deleted_at or utc_now()
    row.provision_state = _PROVISION_READY
    row.provision_token = None
    row.provision_completed_token = None
    row.provision_started_at = None
    _mark_incomplete_workspace(row, row.workspace_dir)


def _mark_incomplete_workspace(row: AgentRegistryModel, workspace_dir: str) -> None:
    recovery = _IncompleteWorkspaceRecovery(workspace_dir=workspace_dir)
    row.provision_previous_json = recovery.model_dump(mode="json")


def _canonical_recovery_layout(agent_id: str, workspace_dir: str) -> BusinessAgentLayout | None:
    workspace = Path(workspace_dir)
    if not workspace.is_absolute() or ".." in workspace.parts or len(workspace.parents) < 3:
        return None
    data_dir = workspace.parents[2]
    try:
        layout = business_agent_layout(data_dir, agent_id)
    except ValueError:
        return None
    return layout if layout.workspace == workspace else None


def _is_never_public_quarantine(row: AgentRegistryModel, layout: BusinessAgentLayout) -> bool:
    if (
        not row.deleted_at
        or row.provision_state != _PROVISION_READY
        or row.provision_token is not None
        or row.provision_completed_token is not None
        or row.provision_started_at is not None
        or row.workspace_dir != str(layout.workspace)
        or row.provision_previous_json is None
    ):
        return False
    try:
        recovery = _parse_workspace_recovery(row.provision_previous_json)
    except DataIntegrityError:
        return False
    return recovery.workspace_dir == str(layout.workspace)


def _layout_root_is_absent(layout: BusinessAgentLayout) -> bool:
    for parent in (layout.root.parents[1], layout.root.parent):
        try:
            observed = os.lstat(parent)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(observed.st_mode):
            return False
    try:
        os.lstat(layout.root)
    except FileNotFoundError:
        return True
    return False
