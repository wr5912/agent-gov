from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy.orm import sessionmaker

from ..agent_profiles import AgentRuntimeProfile, read_requires_web_hitl
from ..agent_registry_db import AgentRegistryModel
from ..errors import BusinessRuleViolation, ConflictError, DataIntegrityError, NotFoundError
from ..protected_business_agents import is_protected_business_agent
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
    status: str = "active"
    origin: str = "user"  # #26：seed（声明式基线，禁删）vs user（用户创建，可 tombstone 删除）
    requires_web_hitl: bool = False  # 从 workspace project settings permissions.ask 派生的只读观测值


@dataclass(frozen=True)
class AgentProvisionReservation:
    """Opaque ownership claim for one DB + workspace provisioning saga."""

    agent_id: str
    token: str
    created_new: bool
    require_workspace_absent: bool = False


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
    origin: _NonEmptyText
    deleted_at: _NonEmptyText
    workspace_recovery: _IncompleteWorkspaceRecovery | None = None


class AgentRegistryStore:
    """业务 Agent 身份注册表存储（AGV-004/022 基座）。

    只登记业务 Agent（被治理对象）；治理 Agent（闭环执行者）不入注册表。
    `sync_business_agents` 幂等，可重复调用而不重复登记。
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def sync_business_agents(self, profiles: dict[str, AgentRuntimeProfile], *, seed_agent_ids: frozenset[str] = frozenset()) -> None:
        with self._session_factory.begin() as db:
            for profile in profiles.values():
                if profile.category != "business":
                    continue
                # 业务 Agent（含预制 main-agent）以 profile.name 为身份；origin 以 seed 目录为准
                # 区分声明式基线 vs 用户创建（#26）。
                origin = "seed" if profile.name in seed_agent_ids else "user"
                existing = db.get(AgentRegistryModel, profile.name)
                if existing is not None:
                    # 未完成创建是内部 saga intent；磁盘发现不得把它提前 finalize 或改写。
                    if (existing.provision_state or _PROVISION_READY) != _PROVISION_READY:
                        continue
                    # #26：用户已删除（tombstone）的 Agent 不因磁盘 workspace 仍在而被复活。
                    if existing.deleted_at:
                        continue
                    # ⑤：已存在记录若 workspace_dir 漂移（升级后路径迁移）同步更新；origin 以 seed 目录校正。
                    if existing.workspace_dir != str(profile.workspace_dir):
                        existing.workspace_dir = str(profile.workspace_dir)
                    existing.origin = origin
                    continue
                db.add(
                    AgentRegistryModel(
                        agent_id=profile.name,
                        name=profile.name,
                        category=profile.category,
                        workspace_dir=str(profile.workspace_dir),
                        created_at=utc_now(),
                        origin=origin,
                        provision_state=_PROVISION_READY,
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

    def create_business_agent(self, *, name: str, agent_id: str, workspace_dir: str) -> AgentRegistryRecord:
        """注册一个业务 Agent 身份（被治理对象）。活跃 agent_id 重复拒绝，空 name 拒绝。

        #26：若该 agent_id 是被 tombstone 删除的旧行（deleted_at 非空），允许复用——清 tombstone
        重置为新建的 user Agent，使删除后的 id 可重新创建（否则 id 永久不可用）。
        """
        clean_name = name.strip()
        if not clean_name:
            raise BusinessRuleViolation("Business agent name cannot be empty")
        created_at = utc_now()
        with self._session_factory.begin() as db:
            existing = db.get(AgentRegistryModel, agent_id)
            if existing is not None:
                if (existing.provision_state or _PROVISION_READY) != _PROVISION_READY or not existing.deleted_at:
                    raise ConflictError(f"Business agent already exists: {agent_id}")
                if existing.provision_previous_json is not None:
                    _parse_workspace_recovery(existing.provision_previous_json)
                    raise ConflictError(f"Business agent {agent_id} has an incomplete workspace; retry through safe provisioning")
                existing.deleted_at = None
                existing.name = clean_name
                existing.category = "business"
                existing.workspace_dir = workspace_dir
                existing.origin = "user"
                existing.status = "active"
                existing.created_at = created_at
                existing.provision_state = _PROVISION_READY
                existing.provision_token = None
                existing.provision_started_at = None
                existing.provision_previous_json = None
                return _record(existing)
            db.add(
                AgentRegistryModel(
                    agent_id=agent_id,
                    name=clean_name,
                    category="business",
                    workspace_dir=workspace_dir,
                    created_at=created_at,
                    origin="user",  # #26：API 创建 = 用户来源（可 tombstone 删除）
                    provision_state=_PROVISION_READY,
                )
            )
        return AgentRegistryRecord(
            agent_id=agent_id,
            name=clean_name,
            category="business",
            workspace_dir=workspace_dir,
            created_at=created_at,
            origin="user",
            requires_web_hitl=read_requires_web_hitl(Path(workspace_dir)),
        )

    def reserve_business_agent(self, *, name: str, agent_id: str, workspace_dir: str) -> AgentProvisionReservation:
        """Persist an invisible, exclusive creation intent before touching the workspace."""
        clean_name = name.strip()
        if not clean_name:
            raise BusinessRuleViolation("Business agent name cannot be empty")
        now = utc_now()
        token = uuid4().hex
        created_new = False
        require_workspace_absent = False
        with self._session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.get(AgentRegistryModel, agent_id)
            if row is None:
                created_new = True
                db.add(
                    AgentRegistryModel(
                        agent_id=agent_id,
                        name=clean_name,
                        category="business",
                        workspace_dir=workspace_dir,
                        created_at=now,
                        status="active",
                        origin="user",
                        provision_state=_PROVISIONING,
                        provision_token=token,
                        provision_started_at=now,
                    )
                )
            else:
                if (row.provision_state or _PROVISION_READY) != _PROVISION_READY or not row.deleted_at:
                    raise ConflictError(f"Business agent already exists or is being provisioned: {agent_id}")
                validate_transition("agent_provision", _PROVISION_READY, _PROVISIONING)
                previous = _snapshot_row(row)
                recovery = previous.workspace_recovery
                if recovery is not None and recovery.workspace_dir != workspace_dir:
                    raise ConflictError(f"Business agent {agent_id} has incomplete workspace residue at a different path")
                require_workspace_absent = recovery is not None
                row.provision_previous_json = previous.model_dump(mode="json")
                row.name = clean_name
                row.category = "business"
                row.workspace_dir = workspace_dir
                row.created_at = now
                row.status = "active"
                row.origin = "user"
                row.provision_state = _PROVISIONING
                row.provision_token = token
                row.provision_started_at = now
            db.flush()
        return AgentProvisionReservation(
            agent_id=agent_id,
            token=token,
            created_new=created_new,
            require_workspace_absent=require_workspace_absent,
        )

    def finalize_business_agent(self, reservation: AgentProvisionReservation) -> AgentRegistryRecord:
        """Publish one reserved row only after its complete workspace is durable."""
        with self._session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = _owned_reservation(db.get(AgentRegistryModel, reservation.agent_id), reservation)
            validate_transition("agent_provision", _PROVISIONING, _PROVISION_READY)
            row.deleted_at = None
            row.provision_state = _PROVISION_READY
            row.provision_token = None
            row.provision_started_at = None
            row.provision_previous_json = None
            db.flush()
        return _record(row)

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
        """Fail closed only after a provisioning heartbeat has expired."""
        recovery_now = runtime_operation_heartbeat(now=now)
        recovered = 0
        with self._session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            rows = db.query(AgentRegistryModel).filter(AgentRegistryModel.provision_state == _PROVISIONING).all()
            for row in rows:
                if not runtime_operation_is_stale(row.provision_started_at, now=recovery_now):
                    continue
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
                recovered += 1
        return recovered

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

    def delete_business_agent(self, agent_id: str) -> AgentRegistryRecord:
        """把业务 Agent 标记为已删除（tombstone），使其立即不可见且重启不复活。

        保护只认受保护名单，不看 `origin`：origin 是「出生来源」的派生投影，会随运行态 seed
        catalog 内容漂移，用它决定删除权限会让保护也跟着漂。main-agent 也不再特判——它是可
        删除的普通业务 Agent。

        本方法只动注册表。磁盘与运行态 seed 的清理由删除服务在事务提交后执行——rmtree 不可
        回滚，放进事务块意味着事务回滚后磁盘已经回不来（见 AGENTS.md 的事务副作用约束）。
        删除前的影响面提示由路由层给出，避免无声删除治理对象。
        """
        if is_protected_business_agent(agent_id):
            raise BusinessRuleViolation(
                f"Business agent '{agent_id}' is protected: its configuration and seed live in the project "
                f"repository and can only be removed through a reviewed repository change"
            )
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            if row is None or not _is_public(row):
                raise NotFoundError(f"Business agent not found: {agent_id}")
            record = _record(row)
            row.deleted_at = utc_now()  # tombstone：sync/discover 均跳过，重启不复活
        return record


def _record(row: AgentRegistryModel) -> AgentRegistryRecord:
    return AgentRegistryRecord(
        agent_id=row.agent_id,
        name=row.name,
        category=row.category,
        workspace_dir=row.workspace_dir,
        created_at=row.created_at,
        status=row.status or "active",
        origin=row.origin or "user",
        requires_web_hitl=read_requires_web_hitl(Path(row.workspace_dir)),
    )


def _is_public(row: AgentRegistryModel) -> bool:
    return not row.deleted_at and (row.provision_state or _PROVISION_READY) == _PROVISION_READY


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
        origin=row.origin,
        deleted_at=row.deleted_at,
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
    row.origin = snapshot.origin
    row.deleted_at = snapshot.deleted_at
    row.provision_state = _PROVISION_READY
    row.provision_token = None
    row.provision_started_at = None
    row.provision_previous_json = snapshot.workspace_recovery.model_dump(mode="json") if snapshot.workspace_recovery is not None else None


def _tombstone_incomplete(row: AgentRegistryModel) -> None:
    validate_transition("agent_provision", row.provision_state, _PROVISION_READY)
    row.deleted_at = row.deleted_at or utc_now()
    row.provision_state = _PROVISION_READY
    row.provision_token = None
    row.provision_started_at = None
    _mark_incomplete_workspace(row, row.workspace_dir)


def _mark_incomplete_workspace(row: AgentRegistryModel, workspace_dir: str) -> None:
    recovery = _IncompleteWorkspaceRecovery(workspace_dir=workspace_dir)
    row.provision_previous_json = recovery.model_dump(mode="json")
