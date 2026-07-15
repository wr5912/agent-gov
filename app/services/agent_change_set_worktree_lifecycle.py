from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.runtime.agent_admission import AgentAdmissionError
from app.runtime.agent_git_store import AgentGitError
from app.runtime.agent_maintenance_db import AgentWorktreeCleanupTaskModel
from app.runtime.agent_ownership import require_persisted_agent_id
from app.runtime.improvement_db import ExecutionRecordModel
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel, utc_now
from app.runtime.state_machines import validate_transition

_CLEANUP_CLAIM_SECONDS = 120


class _GovernanceService(Protocol):
    feedback_store: Any

    def get_change_set(self, change_set_id: str) -> JsonObject | None: ...

    def _store_for(self, agent_id: str | None) -> Any: ...

    def _transition_change_set(
        self,
        change_set_id: str,
        status: str,
        *,
        fields: JsonObject,
        action: str,
        operator: str,
        transaction_mutation: Callable[[object], None] | None = None,
    ) -> JsonObject: ...

    def retry_worktree_cleanup(
        self,
        change_set_id: str,
        *,
        operator: str,
        force: bool,
    ) -> JsonObject: ...


def ensure_worktree_cleanup_task(
    db: Any,
    *,
    change_set_id: str,
    agent_id: str,
    delete_branch: bool,
    now: str | None = None,
) -> AgentWorktreeCleanupTaskModel:
    current = now or utc_now()
    task = db.get(AgentWorktreeCleanupTaskModel, change_set_id)
    if task is not None:
        return task
    task = AgentWorktreeCleanupTaskModel(
        change_set_id=change_set_id,
        agent_id=agent_id,
        status="pending",
        delete_branch=delete_branch,
        attempt_count=0,
        claim_token=None,
        claim_generation=0,
        claim_expires_at=None,
        next_retry_at=None,
        last_error_json={},
        created_at=current,
        updated_at=current,
        completed_at=None,
    )
    db.add(task)
    return task


def pending_cleanup_projection() -> JsonObject:
    return {
        "worktree_cleanup_pending": True,
        "worktree_cleanup": {
            "status": "pending",
            "attempt_count": 0,
            "last_error": None,
            "next_retry_at": None,
        },
    }


def abandon_change_set_and_cleanup(
    service: _GovernanceService,
    change_set_id: str,
    *,
    operator: str,
    note: str | None,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    change_set = service.get_change_set(change_set_id)
    if change_set is None:
        raise _governance_error(404, "Agent change set not found")
    reason = str(change_set.get("abandon_note") or note or "关联 change set 已放弃")
    delete_branch = not bool(change_set.get("candidate_commit_sha"))
    if change_set.get("status") != "abandoned":
        fields = {"abandon_note": note, **pending_cleanup_projection()}
        change_set = service._transition_change_set(
            change_set_id,
            "abandoned",
            fields=fields,
            action="abandoned",
            operator=operator,
            transaction_mutation=lambda db: _prepare_abandoned_cleanup_in_transaction(
                db,
                change_set_id,
                reason=reason,
                agent_id=require_persisted_agent_id(change_set.get("agent_id"), entity=f"Agent change set {change_set_id}"),
                delete_branch=delete_branch,
            ),
        )
    else:
        _prepare_abandoned_cleanup(
            service,
            change_set_id,
            reason=reason,
            agent_id=require_persisted_agent_id(change_set.get("agent_id"), entity=f"Agent change set {change_set_id}"),
            delete_branch=delete_branch,
        )
    return execute_worktree_cleanup(
        service,
        change_set_id,
        force=True,
        assert_maintenance_active=assert_maintenance_active,
    )


def cleanup_published_change_set(
    service: _GovernanceService,
    change_set_id: str,
    release: JsonObject,
    *,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    change_set = service.get_change_set(change_set_id)
    if change_set is None or change_set.get("status") != "published":
        raise _governance_error(409, "Published Agent change set metadata is unavailable for worktree cleanup")
    with service.feedback_store.Session.begin() as db:
        ensure_worktree_cleanup_task(
            db,
            change_set_id=change_set_id,
            agent_id=require_persisted_agent_id(change_set.get("agent_id"), entity=f"Agent change set {change_set_id}"),
            delete_branch=True,
        )
    execute_worktree_cleanup(
        service,
        change_set_id,
        force=True,
        assert_maintenance_active=assert_maintenance_active,
    )
    return release


def execute_worktree_cleanup(
    service: _GovernanceService,
    change_set_id: str,
    *,
    force: bool,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    claim = _claim_cleanup_task(service, change_set_id, force=force)
    if claim is None:
        change_set = service.get_change_set(change_set_id)
        if change_set is None:
            raise _governance_error(404, "Agent change set not found")
        return change_set
    token, generation, agent_id, delete_branch = claim
    try:
        assert_maintenance_active()
        service._store_for(agent_id).remove_worktree(change_set_id, delete_branch=delete_branch)
    except (AgentGitError, AgentAdmissionError, OSError) as exc:
        _fail_cleanup_task(
            service,
            change_set_id,
            token=token,
            generation=generation,
            error=exc,
        )
        raise _governance_error(
            409,
            "Agent change set reached its terminal state, but worktree cleanup is pending; retry the request",
        ) from exc
    try:
        _complete_cleanup_task(
            service,
            change_set_id,
            token=token,
            generation=generation,
        )
    except SQLAlchemyError as exc:
        raise _governance_error(
            409,
            "Agent worktree was removed, but durable cleanup metadata is pending reconciliation",
        ) from exc
    change_set = service.get_change_set(change_set_id)
    if change_set is None:
        raise _governance_error(404, "Agent change set not found")
    return change_set


def list_reconcilable_cleanup_ids(service: _GovernanceService, *, limit: int = 100) -> list[str]:
    now = utc_now()
    with service.feedback_store.Session() as db:
        return list(
            db.scalars(
                select(AgentWorktreeCleanupTaskModel.change_set_id)
                .where(
                    or_(
                        AgentWorktreeCleanupTaskModel.status.in_({"pending", "failed"}),
                        (
                            (AgentWorktreeCleanupTaskModel.status == "claimed")
                            & (AgentWorktreeCleanupTaskModel.claim_expires_at.is_not(None))
                            & (AgentWorktreeCleanupTaskModel.claim_expires_at <= now)
                        ),
                    ),
                    or_(
                        AgentWorktreeCleanupTaskModel.next_retry_at.is_(None),
                        AgentWorktreeCleanupTaskModel.next_retry_at <= now,
                    ),
                )
                .order_by(AgentWorktreeCleanupTaskModel.updated_at.asc())
                .limit(limit)
            ).all()
        )


def reconcile_worktree_cleanup_tasks(
    service: _GovernanceService,
    *,
    limit: int = 100,
) -> JsonObject:
    completed: list[str] = []
    failed: list[str] = []
    for change_set_id in list_reconcilable_cleanup_ids(service, limit=limit):
        try:
            service.retry_worktree_cleanup(
                change_set_id,
                operator="startup-reconciler",
                force=False,
            )
        except Exception:  # noqa: BLE001 - each durable task retains its own failure detail
            failed.append(change_set_id)
        else:
            completed.append(change_set_id)
    return {"completed": completed, "failed": failed}


def _prepare_abandoned_cleanup(
    service: _GovernanceService,
    change_set_id: str,
    *,
    reason: str,
    agent_id: str,
    delete_branch: bool,
) -> None:
    with service.feedback_store.Session.begin() as db:
        change_set = db.get(AgentChangeSetModel, change_set_id)
        if change_set is None or change_set.status != "abandoned":
            raise _governance_error(409, "Agent change set is not ready for abandoned worktree cleanup")
        _prepare_abandoned_cleanup_in_transaction(
            db,
            change_set_id,
            reason=reason,
            agent_id=agent_id,
            delete_branch=delete_branch,
        )


def _prepare_abandoned_cleanup_in_transaction(
    db: Any,
    change_set_id: str,
    *,
    reason: str,
    agent_id: str,
    delete_branch: bool,
) -> None:
    _cancel_applying_execution_claim_in_transaction(db, change_set_id, reason=reason)
    ensure_worktree_cleanup_task(
        db,
        change_set_id=change_set_id,
        agent_id=agent_id,
        delete_branch=delete_branch,
    )


def _claim_cleanup_task(
    service: _GovernanceService,
    change_set_id: str,
    *,
    force: bool,
) -> tuple[str, int, str, bool] | None:
    now = utc_now()
    expires_at = _after_seconds(now, _CLEANUP_CLAIM_SECONDS)
    with service.feedback_store.Session.begin() as db:
        task = db.get(AgentWorktreeCleanupTaskModel, change_set_id)
        if task is None:
            raise _governance_error(409, "Agent change set has no durable worktree cleanup task")
        if task.status == "completed":
            return None
        if task.status == "claimed" and task.claim_expires_at and task.claim_expires_at > now:
            raise _governance_error(409, "Agent worktree cleanup is already claimed")
        if not force and task.next_retry_at and task.next_retry_at > now:
            return None
        token = str(uuid.uuid4())
        generation = int(task.claim_generation or 0) + 1
        changed = db.execute(
            update(AgentWorktreeCleanupTaskModel)
            .where(
                AgentWorktreeCleanupTaskModel.change_set_id == change_set_id,
                AgentWorktreeCleanupTaskModel.status == task.status,
                AgentWorktreeCleanupTaskModel.claim_generation == task.claim_generation,
            )
            .values(
                status="claimed",
                attempt_count=int(task.attempt_count or 0) + 1,
                claim_token=token,
                claim_generation=generation,
                claim_expires_at=expires_at,
                next_retry_at=None,
                updated_at=now,
            )
        ).rowcount
        if changed != 1:
            raise _governance_error(409, "Agent worktree cleanup claim changed concurrently")
        db.expire_all()
        claimed = db.get(AgentWorktreeCleanupTaskModel, change_set_id)
        if claimed is None:
            raise _governance_error(409, "Agent worktree cleanup task disappeared")
        _project_cleanup_task(db, claimed)
        return token, generation, claimed.agent_id, bool(claimed.delete_branch)


def _complete_cleanup_task(
    service: _GovernanceService,
    change_set_id: str,
    *,
    token: str,
    generation: int,
) -> None:
    now = utc_now()
    with service.feedback_store.Session.begin() as db:
        changed = db.execute(
            update(AgentWorktreeCleanupTaskModel)
            .where(
                AgentWorktreeCleanupTaskModel.change_set_id == change_set_id,
                AgentWorktreeCleanupTaskModel.status == "claimed",
                AgentWorktreeCleanupTaskModel.claim_token == token,
                AgentWorktreeCleanupTaskModel.claim_generation == generation,
            )
            .values(
                status="completed",
                claim_token=None,
                claim_expires_at=None,
                next_retry_at=None,
                last_error_json={},
                updated_at=now,
                completed_at=now,
            )
        ).rowcount
        if changed != 1:
            raise _governance_error(409, "Agent worktree cleanup claim was fenced before completion")
        db.expire_all()
        task = db.get(AgentWorktreeCleanupTaskModel, change_set_id)
        if task is not None:
            _project_cleanup_task(db, task)


def _fail_cleanup_task(
    service: _GovernanceService,
    change_set_id: str,
    *,
    token: str,
    generation: int,
    error: BaseException,
) -> None:
    now = utc_now()
    with service.feedback_store.Session.begin() as db:
        task = db.get(AgentWorktreeCleanupTaskModel, change_set_id)
        if task is None or task.status != "claimed" or task.claim_token != token or task.claim_generation != generation:
            return
        task.status = "failed"
        task.claim_token = None
        task.claim_expires_at = None
        task.next_retry_at = _after_seconds(now, min(300, 2 ** min(task.attempt_count, 8)))
        task.last_error_json = {
            "type": type(error).__name__,
            "detail": str(error),
            "updated_at": now,
        }
        task.updated_at = now
        _project_cleanup_task(db, task)


def _project_cleanup_task(db: Any, task: AgentWorktreeCleanupTaskModel) -> None:
    change_set = db.get(AgentChangeSetModel, task.change_set_id)
    if change_set is None:
        return
    payload = dict(change_set.payload_json or {})
    payload.update(
        {
            "worktree_cleanup_pending": task.status != "completed",
            "worktree_cleanup": {
                "status": task.status,
                "attempt_count": task.attempt_count,
                "last_error": dict(task.last_error_json or {}) or None,
                "next_retry_at": task.next_retry_at,
                "completed_at": task.completed_at,
            },
            "updated_at": task.updated_at,
        }
    )
    change_set.updated_at = task.updated_at
    change_set.payload_json = payload


def _cancel_applying_execution_claim_in_transaction(db: Any, change_set_id: str, *, reason: str) -> None:
    row = (
        db.query(ExecutionRecordModel)
        .filter(
            ExecutionRecordModel.change_set_id == change_set_id,
            ExecutionRecordModel.status == "applying",
        )
        .one_or_none()
    )
    if row is None:
        return
    validate_transition("improvement_execution", row.status, "draft")
    changed = db.execute(
        update(ExecutionRecordModel)
        .where(
            ExecutionRecordModel.execution_id == row.execution_id,
            ExecutionRecordModel.status == "applying",
            ExecutionRecordModel.claim_token == row.claim_token,
            ExecutionRecordModel.claim_generation == row.claim_generation,
        )
        .values(
            status="draft",
            summary=f"执行申请已取消：{reason}。",
            changes_applied_json=[],
            agent_version="",
            applied_agent_version_id="",
            applied_diff_json={},
            claim_token="",
            claim_expires_at="",
            updated_at=utc_now(),
        )
    ).rowcount
    if changed != 1:
        raise _governance_error(409, "Execution claim changed during change set abandonment; retry the request")


def _after_seconds(now: str, seconds: int) -> str:
    current = datetime.fromisoformat(now)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current + timedelta(seconds=seconds)).isoformat()


def _governance_error(status_code: int, detail: str) -> Exception:
    from app.services.agent_governance import AgentGovernanceError

    return AgentGovernanceError(status_code, detail)
