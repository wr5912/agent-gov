from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.runtime.agent_admission import AgentAdmissionError, AgentMaintenanceClaim
from app.runtime.agent_git_store import AgentGitError
from app.runtime.agent_maintenance_db import AgentReleaseOperationModel
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentReleaseModel, utc_now
from app.runtime.state_machines import validate_transition
from app.services.agent_change_set_worktree_lifecycle import cleanup_published_change_set
from app.services.agent_governance_projections import release_to_payload
from app.services.agent_publication import PublicationFinalizationLost, reconcile_publication_failure

logger = logging.getLogger(__name__)


class _GovernanceService(Protocol):
    feedback_store: Any
    version_maintenance: Any

    def get_change_set(self, change_set_id: str) -> JsonObject | None: ...

    def get_release(self, release_id: str) -> JsonObject | None: ...

    def _store_for(self, agent_id: str | None) -> Any: ...

    def _normalize_agent_id(self, agent_id: str | None) -> str: ...

    def _ref_policy_validator(self, store: Any, agent_id: str) -> Callable[[str], None]: ...

    def _published_release(self, change_set: JsonObject, *, requested_tag_name: str | None) -> JsonObject: ...

    def _reserve_publication_intent(
        self,
        change_set_id: str,
        *,
        operator: str,
        tag_name: str | None,
        note: str | None,
        force: bool,
    ) -> Any: ...

    def _finalize_publication(self, intent: Any, *, archive: JsonObject) -> JsonObject: ...

    def _add_event_row(self, *args: Any, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class _OperationClaim:
    operation_id: str
    token: str
    generation: int
    kind: str
    release_id: str
    agent_id: str
    expected_head_sha: str
    target_commit_sha: str
    status: str
    git_result: JsonObject


def publish_change_set(
    service: _GovernanceService,
    change_set_id: str,
    *,
    operator: str,
    tag_name: str | None,
    note: str | None,
    force: bool,
) -> JsonObject:
    change_set = service.get_change_set(change_set_id)
    if change_set is None:
        raise _error(404, "Agent change set not found")
    agent_id = service._normalize_agent_id(str(change_set.get("agent_id") or ""))
    try:
        with service.version_maintenance.lease(
            agent_id=agent_id,
            kind="publish",
            owner_id=f"{operator}:{change_set_id}",
        ) as lease:
            result = _publish_change_set_locked(
                service,
                change_set_id,
                operator=operator,
                tag_name=tag_name,
                note=note,
                force=force,
                assert_maintenance_active=lease.assert_active,
            )
            lease.check()
            return result
    except AgentAdmissionError as exc:
        raise _error(409, str(exc)) from exc


def _publish_change_set_locked(
    service: _GovernanceService,
    change_set_id: str,
    *,
    operator: str,
    tag_name: str | None,
    note: str | None,
    force: bool,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    change_set = service.get_change_set(change_set_id)
    if change_set is None:
        raise _error(404, "Agent change set not found")
    if change_set["status"] == "published":
        release = service._published_release(change_set, requested_tag_name=tag_name)
        return cleanup_published_change_set(
            service,
            change_set_id,
            release,
            assert_maintenance_active=assert_maintenance_active,
        )
    if tag_name and change_set["status"] != "publishing":
        candidate = str(change_set.get("candidate_commit_sha") or "")
        if not candidate:
            raise _error(409, "Agent change set has no candidate commit")
        try:
            service._store_for(change_set.get("agent_id")).validate_publication_target(candidate, tag_name)
        except AgentGitError as exc:
            raise _error(409, f"Agent publish preflight failed: {exc}") from exc
    intent = service._reserve_publication_intent(
        change_set_id,
        operator=operator,
        tag_name=tag_name,
        note=note,
        force=force,
    )
    store = service._store_for(intent.agent_id)
    try:
        assert_maintenance_active()
        result = store.publish_commit(
            intent.commit_sha,
            tag_name=intent.tag_name,
            message=intent.note or f"Publish {change_set_id}",
            validate_ref=service._ref_policy_validator(store, intent.agent_id),
        )
        assert_maintenance_active()
    except (AgentGitError, AgentAdmissionError) as exc:
        cancelled = reconcile_publication_failure(
            service.feedback_store.Session,
            store,
            intent=intent,
            detail=str(exc),
            updated_at=utc_now(),
            add_event=service._add_event_row,
        )
        suffix = "; publication intent was cancelled before side effects" if cancelled else ""
        raise _error(409, f"Agent publish failed: {exc}{suffix}") from exc
    archive = result.get("archive") if isinstance(result.get("archive"), dict) else {}
    try:
        release = service._finalize_publication(intent, archive=archive)
        return cleanup_published_change_set(
            service,
            change_set_id,
            release,
            assert_maintenance_active=assert_maintenance_active,
        )
    except (PublicationFinalizationLost, SQLAlchemyError) as exc:
        raise _error(
            409,
            "Agent Git publication completed, but release metadata is pending reconciliation; retry publish",
        ) from exc


def rollback_release(
    service: _GovernanceService,
    release_id: str,
    *,
    operator: str,
    note: str | None,
) -> JsonObject:
    result = _run_release_operation(
        service,
        release_id,
        kind="rollback",
        operator=operator,
        note=note,
    )
    return result["release"]


def restore_release(
    service: _GovernanceService,
    release_id: str,
    *,
    operator: str,
    note: str | None,
) -> JsonObject:
    result = _run_release_operation(
        service,
        release_id,
        kind="restore",
        operator=operator,
        note=note,
    )
    return {
        "schema_version": "agent-release-restore/v1",
        "release": result["release"],
        "restore_result": {
            **result["git_result"],
            "operator": operator,
            "note": note,
        },
    }


def reconcile_release_operations(
    service: _GovernanceService,
    *,
    limit: int = 100,
    now: str | None = None,
) -> JsonObject:
    """Reconcile expired rollback and restore intents after process interruption."""
    cutoff = now or utc_now()
    with service.feedback_store.Session() as db:
        candidates = list(
            db.scalars(
                select(AgentReleaseOperationModel.operation_id)
                .where(
                    AgentReleaseOperationModel.operation_kind.in_({"rollback", "restore"}),
                    AgentReleaseOperationModel.status.in_({"reserved", "git_applied"}),
                    AgentReleaseOperationModel.claim_expires_at.is_not(None),
                    AgentReleaseOperationModel.claim_expires_at <= cutoff,
                )
                .order_by(AgentReleaseOperationModel.created_at)
                .limit(max(1, limit))
            ).all()
        )
    summary: JsonObject = {"completed": [], "failed": [], "deferred": []}
    for operation_id in candidates:
        _reconcile_release_operation(service, str(operation_id), cutoff=cutoff, summary=summary)
    return summary


def _reconcile_release_operation(
    service: _GovernanceService,
    operation_id: str,
    *,
    cutoff: str,
    summary: JsonObject,
) -> None:
    with service.feedback_store.Session() as db:
        operation = db.get(AgentReleaseOperationModel, operation_id)
        if operation is None:
            return
        agent_id = operation.agent_id
        operation_kind = operation.operation_kind
        owner_id = f"reconciler:{operation.operator}:{operation_id}"
    claim: _OperationClaim | None = None
    try:
        with service.version_maintenance.lease(
            agent_id=agent_id,
            kind=f"{operation_kind}_reconcile",
            owner_id=owner_id,
        ) as lease:
            claim = _claim_release_operation_for_reconciliation(
                service,
                operation_id=operation_id,
                maintenance_claim=lease.claim,
                cutoff=cutoff,
            )
            if claim is None:
                _summary_append(summary, "deferred", operation_id)
                return
            store = service._store_for(claim.agent_id)
            current_head = str(store.current_commit_sha() or "")
            if not current_head:
                raise AgentGitError("Agent Git repository is not initialized")
            lease.assert_active()
            git_result = _apply_or_reconcile_git(service, store, claim, current_head=current_head)
            lease.assert_active()
            _mark_git_applied(service, claim, git_result=git_result)
            _complete_release_operation(service, claim, git_result=git_result)
            lease.check()
        _summary_append(summary, "completed", operation_id)
    except AgentAdmissionError:
        _summary_append(summary, "deferred", operation_id)
    except AgentGitError as exc:
        if claim is not None:
            try:
                _mark_release_operation_failed(service, claim, detail=str(exc))
            except Exception:
                logger.exception(
                    "event=agent_release.reconcile_failure_persistence_failed operation_id=%s",
                    operation_id,
                )
                _summary_append(summary, "deferred", operation_id)
                return
        _summary_append(summary, "failed", operation_id)
    except SQLAlchemyError as exc:
        logger.warning(
            "event=agent_release.reconcile_deferred operation_id=%s reason=%s",
            operation_id,
            exc.__class__.__name__,
        )
        _summary_append(summary, "deferred", operation_id)
    except Exception:
        logger.exception(
            "event=agent_release.reconcile_unexpected operation_id=%s",
            operation_id,
        )
        _summary_append(summary, "deferred", operation_id)


def _claim_release_operation_for_reconciliation(
    service: _GovernanceService,
    *,
    operation_id: str,
    maintenance_claim: AgentMaintenanceClaim,
    cutoff: str,
) -> _OperationClaim | None:
    with service.feedback_store.Session.begin() as db:
        operation = db.get(AgentReleaseOperationModel, operation_id)
        if (
            operation is None
            or operation.operation_kind not in {"rollback", "restore"}
            or operation.status not in {"reserved", "git_applied"}
            or not operation.claim_expires_at
            or operation.claim_expires_at > cutoff
        ):
            return None
        operation.claim_token = maintenance_claim.token
        operation.claim_generation = maintenance_claim.generation
        operation.claim_expires_at = maintenance_claim.expires_at
        operation.updated_at = utc_now()
        db.flush()
        return _claim_from_row(operation)


def _summary_append(summary: JsonObject, key: str, operation_id: str) -> None:
    values = summary.get(key)
    if isinstance(values, list):
        values.append(operation_id)


def _run_release_operation(
    service: _GovernanceService,
    release_id: str,
    *,
    kind: str,
    operator: str,
    note: str | None,
) -> JsonObject:
    release = service.get_release(release_id)
    if release is None:
        raise _error(404, "Agent release not found")
    agent_id = service._normalize_agent_id(str(release.get("agent_id") or ""))
    store = service._store_for(agent_id)
    target = _release_operation_target(store, release, kind=kind)
    try:
        with service.version_maintenance.lease(
            agent_id=agent_id,
            kind=kind,
            owner_id=f"{operator}:{release_id}",
        ) as lease:
            current_head = str(store.current_commit_sha() or "")
            if not current_head:
                raise _error(409, "Agent Git repository is not initialized")
            claim, completed = _reserve_release_operation(
                service,
                maintenance_claim=lease.claim,
                release=release,
                kind=kind,
                target_commit_sha=target,
                observed_head_sha=current_head,
                operator=operator,
                note=note,
            )
            if completed is not None:
                return completed
            lease.assert_active()
            git_result = _apply_or_reconcile_git(service, store, claim, current_head=current_head)
            lease.assert_active()
            _mark_git_applied(service, claim, git_result=git_result)
            completed_result = _complete_release_operation(service, claim, git_result=git_result)
            lease.check()
            return completed_result
    except AgentAdmissionError as exc:
        raise _error(409, str(exc)) from exc
    except AgentGitError as exc:
        if "claim" in locals():
            _mark_release_operation_failed(service, claim, detail=str(exc))
        prefix = "rollback" if kind == "rollback" else "release restore"
        raise _error(409, f"Agent {prefix} failed: {exc}") from exc
    except SQLAlchemyError as exc:
        raise _error(
            409,
            "Agent Git version maintenance may have completed, but metadata is pending reconciliation; retry",
        ) from exc


def _release_operation_target(store: Any, release: JsonObject, *, kind: str) -> str:
    if kind == "restore":
        return str(release["commit_sha"])
    target = str(release.get("previous_commit_sha") or "")
    if not target:
        target = str(store.version_summary(str(release["commit_sha"]), reason="rollback_target").get("parent_version_id") or "")
    if not target:
        raise _error(409, "Agent release has no previous commit to roll back to")
    return target


def _reserve_release_operation(
    service: _GovernanceService,
    *,
    maintenance_claim: AgentMaintenanceClaim,
    release: JsonObject,
    kind: str,
    target_commit_sha: str,
    observed_head_sha: str,
    operator: str,
    note: str | None,
) -> tuple[_OperationClaim, JsonObject | None]:
    now = utc_now()
    with service.feedback_store.Session.begin() as db:
        release_row = db.get(AgentReleaseModel, str(release["release_id"]))
        if release_row is None:
            raise _error(404, "Agent release not found")
        operations = _release_operations(
            db,
            release_id=release_row.release_id,
            kind=kind,
        )
        unfinished = next((item for item in operations if item.status != "completed"), None)
        exact_completed = next(
            (
                item
                for item in operations
                if item.status == "completed"
                and item.target_commit_sha == target_commit_sha
                and item.observed_head_sha == observed_head_sha
                and observed_head_sha == target_commit_sha
            ),
            None,
        )
        operation = unfinished or exact_completed
        if operation is not None and operation.status == "completed":
            return _claim_from_row(operation), _completed_operation_payload(service, operation)
        completed = [item for item in operations if item.status == "completed"]
        if operation is None:
            _validate_new_release_operation(
                release_row,
                kind=kind,
                observed_head_sha=observed_head_sha,
                completed=completed,
            )
        if operation is None:
            operation = _new_release_operation(
                release_row,
                maintenance_claim=maintenance_claim,
                kind=kind,
                target_commit_sha=target_commit_sha,
                observed_head_sha=observed_head_sha,
                operator=operator,
                note=note,
                now=now,
            )
            db.add(operation)
            db.flush()
        else:
            _resume_release_operation(
                operation,
                maintenance_claim=maintenance_claim,
                target_commit_sha=target_commit_sha,
                operator=operator,
                note=note,
                now=now,
            )
        return _claim_from_row(operation), None


def _release_operations(
    db: Any,
    *,
    release_id: str,
    kind: str,
) -> list[AgentReleaseOperationModel]:
    return list(
        db.scalars(
            select(AgentReleaseOperationModel)
            .where(
                AgentReleaseOperationModel.release_id == release_id,
                AgentReleaseOperationModel.operation_kind == kind,
            )
            .order_by(AgentReleaseOperationModel.created_at.desc())
        ).all()
    )


def _validate_new_release_operation(
    release: AgentReleaseModel,
    *,
    kind: str,
    observed_head_sha: str,
    completed: list[AgentReleaseOperationModel],
) -> None:
    if kind != "rollback":
        return
    if completed:
        raise _error(
            409,
            "Agent release rollback was already completed and no longer matches the current HEAD",
        )
    if observed_head_sha != release.commit_sha:
        raise _error(
            409,
            "Agent release rollback requires the release commit to be the current workspace HEAD",
        )


def _new_release_operation(
    release: AgentReleaseModel,
    *,
    maintenance_claim: AgentMaintenanceClaim,
    kind: str,
    target_commit_sha: str,
    observed_head_sha: str,
    operator: str,
    note: str | None,
    now: str,
) -> AgentReleaseOperationModel:
    operation_id = f"aro-{uuid.uuid5(uuid.NAMESPACE_URL, f'agentgov:{kind}:{release.release_id}:{observed_head_sha}')}"
    return AgentReleaseOperationModel(
        operation_id=operation_id,
        agent_id=release.agent_id,
        release_id=release.release_id,
        operation_kind=kind,
        status="reserved",
        expected_head_sha=observed_head_sha,
        target_commit_sha=target_commit_sha,
        release_expected_status=release.status,
        release_expected_updated_at=release.updated_at,
        claim_token=maintenance_claim.token,
        claim_generation=maintenance_claim.generation,
        claim_expires_at=maintenance_claim.expires_at,
        operator=operator,
        note=note,
        previous_head_sha=observed_head_sha,
        observed_head_sha=observed_head_sha,
        result_json={},
        error_json={},
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def _resume_release_operation(
    operation: AgentReleaseOperationModel,
    *,
    maintenance_claim: AgentMaintenanceClaim,
    target_commit_sha: str,
    operator: str,
    note: str | None,
    now: str,
) -> None:
    if operation.target_commit_sha != target_commit_sha:
        raise _error(409, "Agent release maintenance target changed during recovery")
    operation.claim_token = maintenance_claim.token
    operation.claim_generation = maintenance_claim.generation
    operation.claim_expires_at = maintenance_claim.expires_at
    operation.operator = operator
    operation.note = note
    operation.updated_at = now
    operation.error_json = {}
    if operation.status == "failed":
        validate_transition("agent_release_operation", operation.status, "reserved")
        operation.status = "reserved"


def _apply_or_reconcile_git(service: _GovernanceService, store: Any, claim: _OperationClaim, *, current_head: str) -> JsonObject:
    if claim.status == "git_applied":
        if current_head != claim.target_commit_sha:
            raise AgentGitError(
                "Agent workspace HEAD changed after the rollback Git effect was persisted "
                f"(expected target {claim.target_commit_sha}, found {current_head})"
            )
        return claim.git_result or _reconciled_git_result(claim)
    if current_head == claim.target_commit_sha:
        return _reconciled_git_result(claim)
    if current_head != claim.expected_head_sha:
        raise AgentGitError(f"Agent workspace HEAD changed during version maintenance (expected {claim.expected_head_sha}, found {current_head})")
    return store.rollback_to_ref(
        claim.target_commit_sha,
        expected_current_ref=claim.expected_head_sha,
        validate_ref=service._ref_policy_validator(store, claim.agent_id),
    )


def _reconciled_git_result(claim: _OperationClaim) -> JsonObject:
    return {
        "previous_commit_sha": claim.expected_head_sha,
        "current_commit_sha": claim.target_commit_sha,
        "rollback_target_ref": claim.target_commit_sha,
        "requires_runtime_restart": True,
        "reconciled_after_interruption": True,
    }


def _mark_git_applied(
    service: _GovernanceService,
    claim: _OperationClaim,
    *,
    git_result: JsonObject,
) -> None:
    now = utc_now()
    with service.feedback_store.Session.begin() as db:
        operation = db.get(AgentReleaseOperationModel, claim.operation_id)
        if (
            operation is None
            or operation.claim_token != claim.token
            or operation.claim_generation != claim.generation
            or operation.status not in {"reserved", "git_applied"}
        ):
            raise AgentGitError("Agent release maintenance claim was fenced before Git result persistence")
        if operation.status == "reserved":
            validate_transition("agent_release_operation", operation.status, "git_applied")
        operation.status = "git_applied"
        operation.observed_head_sha = str(git_result.get("current_commit_sha") or "")
        operation.result_json = git_result
        operation.error_json = {}
        operation.updated_at = now


def _complete_release_operation(
    service: _GovernanceService,
    claim: _OperationClaim,
    *,
    git_result: JsonObject,
) -> JsonObject:
    now = utc_now()
    with service.feedback_store.Session.begin() as db:
        operation = db.get(AgentReleaseOperationModel, claim.operation_id)
        if operation is None or operation.claim_token != claim.token or operation.claim_generation != claim.generation or operation.status != "git_applied":
            raise AgentGitError("Agent release maintenance claim was fenced before completion")
        release = db.get(AgentReleaseModel, claim.release_id)
        if release is None:
            raise _error(404, "Agent release not found")
        if claim.kind == "rollback":
            validate_transition("agent_release", release.status, "rolled_back")
            payload = dict(release.payload_json or {})
            payload.update(
                {
                    "status": "rolled_back",
                    "updated_at": now,
                    "operator": operation.operator,
                    "rollback_result": git_result,
                    "rollback_note": operation.note,
                    "rollback_target_commit_sha": claim.target_commit_sha,
                }
            )
            changed = db.execute(
                update(AgentReleaseModel)
                .where(
                    AgentReleaseModel.release_id == release.release_id,
                    AgentReleaseModel.status == operation.release_expected_status,
                    AgentReleaseModel.updated_at == operation.release_expected_updated_at,
                )
                .values(status="rolled_back", updated_at=now, payload_json=payload)
            ).rowcount
            if changed != 1:
                raise AgentGitError("Agent release metadata changed before rollback completion")
        validate_transition("agent_release_operation", operation.status, "completed")
        operation.status = "completed"
        operation.result_json = git_result
        operation.error_json = {}
        operation.updated_at = now
        operation.completed_at = now
        operation.claim_token = None
        operation.claim_expires_at = None
        db.flush()
        db.expire_all()
        completed_release = db.get(AgentReleaseModel, claim.release_id)
        if completed_release is None:
            raise _error(404, "Agent release not found")
        release_payload = release_to_payload(completed_release)
    return {"release": release_payload, "git_result": git_result}


def _mark_release_operation_failed(
    service: _GovernanceService,
    claim: _OperationClaim,
    *,
    detail: str,
) -> None:
    now = utc_now()
    with service.feedback_store.Session.begin() as db:
        operation = db.get(AgentReleaseOperationModel, claim.operation_id)
        if operation is None or operation.claim_token != claim.token or operation.claim_generation != claim.generation or operation.status == "completed":
            return
        if operation.status != "failed":
            validate_transition("agent_release_operation", operation.status, "failed")
        operation.status = "failed"
        operation.error_json = {"detail": detail, "updated_at": now}
        operation.updated_at = now
        operation.claim_token = None
        operation.claim_expires_at = None
        if claim.kind != "rollback":
            return
        release = db.get(AgentReleaseModel, claim.release_id)
        if release is None or release.status not in {"published", "archived"}:
            return
        validate_transition("agent_release", release.status, "rollback_failed")
        payload = dict(release.payload_json or {})
        payload.update(
            {
                "status": "rollback_failed",
                "updated_at": now,
                "operator": operation.operator,
                "rollback_error": detail,
            }
        )
        release.status = "rollback_failed"
        release.updated_at = now
        release.payload_json = payload
        operation.release_expected_status = "rollback_failed"
        operation.release_expected_updated_at = now


def _completed_operation_payload(
    service: _GovernanceService,
    operation: AgentReleaseOperationModel,
) -> JsonObject:
    release = service.get_release(operation.release_id)
    if release is None:
        raise _error(404, "Agent release not found")
    return {"release": release, "git_result": dict(operation.result_json or {})}


def _claim_from_row(row: AgentReleaseOperationModel) -> _OperationClaim:
    return _OperationClaim(
        operation_id=row.operation_id,
        token=str(row.claim_token or ""),
        generation=int(row.claim_generation or 0),
        kind=row.operation_kind,
        release_id=row.release_id,
        agent_id=row.agent_id,
        expected_head_sha=row.expected_head_sha,
        target_commit_sha=row.target_commit_sha,
        status=row.status,
        git_result=dict(row.result_json or {}),
    )


def _error(status_code: int, detail: str) -> Exception:
    from app.services.agent_governance import AgentGovernanceError

    return AgentGovernanceError(status_code, detail)
