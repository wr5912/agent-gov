from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy import update

from app.runtime.agent_git_store import AgentGitError
from app.runtime.improvement_db import ExecutionRecordModel
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel, utc_now
from app.runtime.state_machines import validate_transition


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


def abandon_change_set_and_cleanup(
    service: _GovernanceService,
    change_set_id: str,
    *,
    operator: str,
    note: str | None,
) -> JsonObject:
    change_set = service.get_change_set(change_set_id)
    if change_set is None:
        raise _governance_error(404, "Agent change set not found")
    reason = str(change_set.get("abandon_note") or note or "关联 change set 已放弃")
    if change_set.get("status") != "abandoned":
        change_set = service._transition_change_set(
            change_set_id,
            "abandoned",
            fields={"abandon_note": note, "worktree_cleanup_pending": True},
            action="abandoned",
            operator=operator,
            transaction_mutation=lambda db: _cancel_applying_execution_claim_in_transaction(
                db,
                change_set_id,
                reason=reason,
            ),
        )
    else:
        _prepare_abandoned_cleanup(service, change_set_id, reason=reason)
    _remove_worktree(service, change_set, delete_branch=not bool(change_set.get("candidate_commit_sha")))
    _mark_cleanup_complete(service, change_set_id)
    return service.get_change_set(change_set_id) or change_set


def cleanup_published_change_set(
    service: _GovernanceService,
    change_set_id: str,
    release: JsonObject,
) -> JsonObject:
    change_set = service.get_change_set(change_set_id)
    if change_set is None or change_set.get("status") != "published":
        raise _governance_error(409, "Published Agent change set metadata is unavailable for worktree cleanup")
    _remove_worktree(service, change_set, delete_branch=True)
    return release


def _remove_worktree(service: _GovernanceService, change_set: JsonObject, *, delete_branch: bool) -> None:
    try:
        service._store_for(change_set.get("agent_id")).remove_worktree(
            str(change_set["change_set_id"]),
            delete_branch=delete_branch,
        )
    except (AgentGitError, OSError) as exc:
        raise _governance_error(
            409,
            "Agent change set reached its terminal state, but worktree cleanup is pending; retry the request",
        ) from exc


def _prepare_abandoned_cleanup(service: _GovernanceService, change_set_id: str, *, reason: str) -> None:
    with service.feedback_store.Session.begin() as db:
        change_set = db.get(AgentChangeSetModel, change_set_id)
        if change_set is None or change_set.status != "abandoned":
            raise _governance_error(409, "Agent change set is not ready for abandoned worktree cleanup")
        _cancel_applying_execution_claim_in_transaction(db, change_set_id, reason=reason)
        _set_cleanup_pending(db, change_set, pending=True)


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


def _mark_cleanup_complete(service: _GovernanceService, change_set_id: str) -> None:
    with service.feedback_store.Session.begin() as db:
        change_set = db.get(AgentChangeSetModel, change_set_id)
        if change_set is None or change_set.status != "abandoned":
            raise _governance_error(409, "Agent change set changed before worktree cleanup completed")
        _set_cleanup_pending(db, change_set, pending=False)


def _set_cleanup_pending(db: Any, change_set: AgentChangeSetModel, *, pending: bool) -> None:
    previous_payload = dict(change_set.payload_json or {})
    if bool(previous_payload.get("worktree_cleanup_pending")) == pending:
        return
    payload = {**previous_payload, "worktree_cleanup_pending": pending, "updated_at": utc_now()}
    changed = db.execute(
        update(AgentChangeSetModel)
        .where(
            AgentChangeSetModel.change_set_id == change_set.change_set_id,
            AgentChangeSetModel.status == change_set.status,
            AgentChangeSetModel.updated_at == change_set.updated_at,
            AgentChangeSetModel.payload_json == previous_payload,
        )
        .values(updated_at=payload["updated_at"], payload_json=payload)
    ).rowcount
    if changed != 1:
        raise _governance_error(409, "Agent change set cleanup state changed concurrently; retry the request")


def _governance_error(status_code: int, detail: str) -> Exception:
    # Local import avoids a module cycle while keeping route-safe error projection.
    from app.services.agent_governance import AgentGovernanceError

    return AgentGovernanceError(status_code, detail)
