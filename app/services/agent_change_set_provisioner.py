from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.runtime.agent_git_store import GitAgentVersionStore, GitWorktreeRef
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetEventModel, AgentChangeSetModel, utc_now
from app.services.agent_version_maintenance import AgentVersionMaintenanceCoordinator

_CHANGE_SET_ID_PATTERN = re.compile(r"agc-[0-9a-fA-F-]{8,64}\Z")


class ChangeSetProvisionConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangeSetSource:
    improvement_id: str
    attribution_id: str | None = None
    attribution_status: str | None = None


def provision_change_set_under_maintenance(
    *,
    session_factory: sessionmaker,
    version_maintenance: AgentVersionMaintenanceCoordinator,
    store_for: Callable[[str], GitAgentVersionStore],
    agent_id: str,
    execution_job_id: str | None,
    base_commit_sha: str | None,
    title: str | None,
    note: str | None,
    operator: str,
    change_set_id: str | None,
    source: ChangeSetSource | None = None,
) -> str:
    lease = version_maintenance.lease(
        agent_id=agent_id,
        kind="change_set_create",
        owner_id=f"change-set:{change_set_id or execution_job_id or uuid.uuid4().hex}",
    )
    acquired = False
    try:
        lease.__enter__()
        acquired = True
        store = store_for(agent_id)
        lease.assert_active()
        return provision_change_set(
            session_factory=session_factory,
            store=store,
            agent_id=agent_id,
            execution_job_id=execution_job_id,
            base_commit_sha=base_commit_sha,
            title=title,
            note=note,
            operator=operator,
            change_set_id=change_set_id,
            source=source,
        )
    finally:
        if acquired:
            lease.close(validate_claim=False)


def provision_change_set(
    *,
    session_factory: sessionmaker,
    store: GitAgentVersionStore,
    agent_id: str,
    execution_job_id: str | None,
    base_commit_sha: str | None,
    title: str | None,
    note: str | None,
    operator: str,
    change_set_id: str | None,
    source: ChangeSetSource | None = None,
) -> str:
    """Create a recoverable Git/DB change-set intent using a caller-stable ID."""
    requested_id = change_set_id or f"agc-{uuid.uuid4()}"
    if not _CHANGE_SET_ID_PATTERN.fullmatch(requested_id):
        raise ChangeSetProvisionConflict("Invalid Agent change set id")
    existing = _existing_change_set(session_factory, requested_id)
    if existing is not None:
        _validate_existing(
            existing,
            agent_id=agent_id,
            execution_job_id=execution_job_id,
            base_commit_sha=base_commit_sha,
            source=source,
        )
        return requested_id
    resolved_base = base_commit_sha or store.current_commit_sha()
    if not resolved_base:
        raise ChangeSetProvisionConflict("Agent Git repository has no base commit")
    worktree = store.create_worktree(requested_id, base_ref=resolved_base)
    now = utc_now()
    payload = _change_set_payload(
        change_set_id=requested_id,
        agent_id=agent_id,
        execution_job_id=execution_job_id,
        title=title,
        note=note,
        now=now,
        worktree=worktree,
        source=source,
    )
    try:
        _insert_change_set(
            session_factory,
            requested_id=requested_id,
            agent_id=agent_id,
            execution_job_id=execution_job_id,
            worktree=worktree,
            now=now,
            operator=operator,
            record_data=payload,
        )
    except IntegrityError:
        existing = _existing_change_set(session_factory, requested_id)
        if existing is None:
            raise
        _validate_existing(
            existing,
            agent_id=agent_id,
            execution_job_id=execution_job_id,
            base_commit_sha=resolved_base,
            source=source,
        )
    return requested_id


def _insert_change_set(
    session_factory: sessionmaker,
    *,
    requested_id: str,
    agent_id: str,
    execution_job_id: str | None,
    worktree: GitWorktreeRef,
    now: str,
    operator: str,
    record_data: Mapping[str, object],
) -> None:
    with session_factory.begin() as db:
        db.add(
            AgentChangeSetModel(
                change_set_id=requested_id,
                agent_id=agent_id,
                created_at=now,
                updated_at=now,
                status="draft",
                execution_job_id=execution_job_id,
                base_commit_sha=worktree.base_commit_sha,
                candidate_commit_sha=None,
                branch_name=worktree.branch_name,
                worktree_path=str(worktree.worktree_path),
                payload_json=dict(record_data),
            )
        )
        db.flush()
        db.add(
            AgentChangeSetEventModel(
                event_id=f"age-{uuid.uuid4()}",
                change_set_id=requested_id,
                action="created",
                operator=operator,
                created_at=now,
                before_json={},
                after_json=dict(record_data),
            )
        )


def _existing_change_set(session_factory: sessionmaker, change_set_id: str) -> AgentChangeSetModel | None:
    with session_factory() as db:
        return db.get(AgentChangeSetModel, change_set_id)


def _validate_existing(
    row: AgentChangeSetModel,
    *,
    agent_id: str,
    execution_job_id: str | None,
    base_commit_sha: str | None,
    source: ChangeSetSource | None,
) -> None:
    if row.agent_id != agent_id:
        raise ChangeSetProvisionConflict("Agent change set id belongs to a different Agent")
    if execution_job_id and row.execution_job_id and row.execution_job_id != execution_job_id:
        raise ChangeSetProvisionConflict("Agent change set id belongs to a different execution")
    if base_commit_sha and row.base_commit_sha != base_commit_sha:
        raise ChangeSetProvisionConflict("Agent change set base commit conflicts with its execution intent")
    payload = dict(row.payload_json or {})
    if source and (payload.get("source_improvement_id") != source.improvement_id or payload.get("source_attribution_id") != source.attribution_id):
        raise ChangeSetProvisionConflict("Agent change set belongs to a different improvement attribution")


def _change_set_payload(
    *,
    change_set_id: str,
    agent_id: str,
    execution_job_id: str | None,
    title: str | None,
    note: str | None,
    now: str,
    worktree: GitWorktreeRef,
    source: ChangeSetSource | None,
) -> JsonObject:
    return {
        "schema_version": "agent-change-set/v1",
        "change_set_id": change_set_id,
        "agent_id": agent_id,
        "created_at": now,
        "updated_at": now,
        "status": "draft",
        "execution_job_id": execution_job_id,
        "base_commit_sha": worktree.base_commit_sha,
        "candidate_commit_sha": None,
        "branch_name": worktree.branch_name,
        "worktree_path": str(worktree.worktree_path),
        "title": title,
        "note": note,
        "diff_summary": {},
        "latest_eval_run_id": None,
        "latest_release_id": None,
        "source_improvement_id": source.improvement_id if source else None,
        "source_attribution_id": source.attribution_id if source else None,
        "source_attribution_status": source.attribution_status if source else None,
    }
