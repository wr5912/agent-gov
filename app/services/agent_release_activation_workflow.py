from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.exc import SQLAlchemyError

from app.runtime.agent_admission import AgentAdmissionError
from app.runtime.agent_git_store import AgentGitError
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import utc_now
from app.runtime_gateway.client import RuntimeUpstreamError
from app.runtime_gateway.release_activation import RuntimeActivationCleanupPending, RuntimeActivationRestartRequired
from app.runtime_gateway.store import RuntimeStoreError
from app.services.agent_change_set_worktree_lifecycle import cleanup_published_change_set
from app.services.agent_publication import (
    PublicationFinalizationLost,
    PublicationIntent,
    reconcile_publication_failure,
    record_publication_error,
    release_projection_matches_intent,
)
from app.services.agent_publication_reservation import PublicationRequest
from app.services.agent_publication_validation import require_existing_publication_request


class _GovernanceService(Protocol):
    feedback_store: Any
    version_maintenance: Any
    release_activator: Any
    release_activation_committer: Any
    release_activation_compensator: Any

    def get_change_set(self, change_set_id: str) -> JsonObject | None: ...

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
        expected_candidate_commit_sha: str,
        expected_diff_digest: str,
        expected_test_run_id: str | None,
        expected_suite_digest: str | None,
    ) -> PublicationIntent: ...

    def _finalize_publication(self, intent: PublicationIntent, *, archive: JsonObject) -> JsonObject: ...

    def _add_event_row(self, *args: Any, **kwargs: Any) -> None: ...


async def publish_change_set(
    service: _GovernanceService,
    change_set_id: str,
    *,
    operator: str,
    tag_name: str | None,
    note: str | None,
    force: bool,
    expected_candidate_commit_sha: str,
    expected_diff_digest: str,
    expected_test_run_id: str | None,
    expected_suite_digest: str | None,
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
            result = await _publish_change_set_locked(
                service,
                change_set_id,
                operator=operator,
                tag_name=tag_name,
                note=note,
                force=force,
                expected_candidate_commit_sha=expected_candidate_commit_sha,
                expected_diff_digest=expected_diff_digest,
                expected_test_run_id=expected_test_run_id,
                expected_suite_digest=expected_suite_digest,
                assert_maintenance_active=lease.assert_active,
            )
            lease.check()
            return result
    except AgentAdmissionError as exc:
        raise _error(409, str(exc)) from exc


async def _publish_change_set_locked(
    service: _GovernanceService,
    change_set_id: str,
    *,
    operator: str,
    tag_name: str | None,
    note: str | None,
    force: bool,
    expected_candidate_commit_sha: str,
    expected_diff_digest: str,
    expected_test_run_id: str | None,
    expected_suite_digest: str | None,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    change_set = service.get_change_set(change_set_id)
    if change_set is None:
        raise _error(404, "Agent change set not found")
    request = PublicationRequest(
        change_set_id=change_set_id,
        operator=operator,
        tag_name=tag_name,
        note=note,
        force=force,
        expected_candidate_commit_sha=expected_candidate_commit_sha,
        expected_diff_digest=expected_diff_digest,
        expected_test_run_id=expected_test_run_id,
        expected_suite_digest=expected_suite_digest,
    )
    existing_intent = require_existing_publication_request(
        change_set,
        force=request.force,
        expected_candidate_commit_sha=request.expected_candidate_commit_sha,
        expected_diff_digest=request.expected_diff_digest,
        expected_test_run_id=request.expected_test_run_id,
        expected_suite_digest=request.expected_suite_digest,
    )
    if change_set["status"] == "published":
        return _resume_published_change_set(
            service,
            change_set,
            existing_intent,
            request,
            assert_maintenance_active=assert_maintenance_active,
        )
    return await _publish_new_change_set(
        service,
        change_set,
        request,
        assert_maintenance_active=assert_maintenance_active,
    )


def _reserve_intent(service: _GovernanceService, request: PublicationRequest) -> PublicationIntent:
    return service._reserve_publication_intent(
        request.change_set_id,
        operator=request.operator,
        tag_name=request.tag_name,
        note=request.note,
        force=request.force,
        expected_candidate_commit_sha=request.expected_candidate_commit_sha,
        expected_diff_digest=request.expected_diff_digest,
        expected_test_run_id=request.expected_test_run_id,
        expected_suite_digest=request.expected_suite_digest,
    )


def _resume_published_change_set(
    service: _GovernanceService,
    change_set: JsonObject,
    existing_intent: PublicationIntent | None,
    request: PublicationRequest,
    *,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    if existing_intent is None:
        raise _error(409, "Published Agent change set has no verifiable publication intent")
    verified_intent = _reserve_intent(service, request)
    try:
        git_identity_matches = service._store_for(verified_intent.agent_id).published_identity_matches(
            verified_intent.commit_sha,
            verified_intent.tag_name,
        )
    except (AgentGitError, OSError, RuntimeError):
        git_identity_matches = False
    if not git_identity_matches:
        raise _error(409, "Published Agent release no longer matches its live Git/tag identity")
    release = service._published_release(change_set, requested_tag_name=request.tag_name)
    if not release_projection_matches_intent(release, verified_intent):
        raise _error(409, "Published Agent release no longer matches its immutable publication intent")
    return cleanup_published_change_set(
        service,
        request.change_set_id,
        release,
        assert_maintenance_active=assert_maintenance_active,
    )


async def _publish_new_change_set(
    service: _GovernanceService,
    change_set: JsonObject,
    request: PublicationRequest,
    *,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    _validate_publication_target(service, change_set, tag_name=request.tag_name)
    if any(
        callback is None
        for callback in (
            service.release_activator,
            service.release_activation_committer,
            service.release_activation_compensator,
        )
    ):
        raise _error(503, "Agent release activation is not configured")
    intent = _reserve_intent(service, request)
    _validate_intent_publication_target(service, intent)
    binding, result = await _activate_and_publish(
        service,
        change_set,
        intent,
        assert_maintenance_active=assert_maintenance_active,
    )
    await _commit_activation_metadata(service, intent, binding)
    return _finalize_activation(
        service,
        request.change_set_id,
        intent,
        binding,
        result,
        assert_maintenance_active=assert_maintenance_active,
    )


def _validate_publication_target(
    service: _GovernanceService,
    change_set: JsonObject,
    *,
    tag_name: str | None,
) -> None:
    candidate = str(change_set.get("candidate_commit_sha") or "")
    if not candidate:
        raise _error(409, "Agent change set has no candidate commit")
    publication_evidence = change_set.get("publication_evidence")
    persisted_tag = publication_evidence.get("tag_name") if isinstance(publication_evidence, dict) else None
    effective_tag = tag_name or (str(persisted_tag) if persisted_tag else f"agent-release-{change_set['change_set_id']}")
    try:
        service._store_for(change_set.get("agent_id")).validate_publication_target(candidate, effective_tag)
    except AgentGitError as exc:
        raise _error(409, f"Agent publish preflight failed: {exc}") from exc


def _validate_intent_publication_target(service: _GovernanceService, intent: Any) -> None:
    try:
        service._store_for(intent.agent_id).validate_publication_target(intent.commit_sha, intent.tag_name)
    except AgentGitError as exc:
        record_publication_error(
            service.feedback_store.Session,
            change_set_id=intent.change_set_id,
            detail=str(exc),
            updated_at=utc_now(),
        )
        raise _error(409, f"Agent publish preflight failed for persisted intent: {exc}") from exc


async def _activate_and_publish(
    service: _GovernanceService,
    change_set: JsonObject,
    intent: Any,
    *,
    assert_maintenance_active: Callable[[], None],
) -> tuple[Any, JsonObject]:
    store = service._store_for(intent.agent_id)
    binding: Any | None = None
    try:
        assert_maintenance_active()
        binding = await service.release_activator(
            agent_id=intent.agent_id,
            agent_version_id=intent.commit_sha,
            candidate_worktree=Path(str(change_set.get("worktree_path") or "")),
        )
        assert_maintenance_active()
        result = await asyncio.to_thread(
            store.publish_commit,
            intent.commit_sha,
            tag_name=intent.tag_name,
            message=intent.note or f"Publish {intent.change_set_id}",
            validate_ref=service._ref_policy_validator(store, intent.agent_id),
        )
        assert_maintenance_active()
    except (AgentGitError, AgentAdmissionError, RuntimeStoreError, RuntimeUpstreamError) as exc:
        await _handle_activation_failure(service, store, intent, binding, exc)
    return binding, result


async def _handle_activation_failure(
    service: _GovernanceService,
    store: Any,
    intent: Any,
    binding: Any | None,
    error: Exception,
) -> None:
    if isinstance(error, RuntimeActivationRestartRequired):
        record_publication_error(
            service.feedback_store.Session,
            change_set_id=intent.change_set_id,
            detail=str(error),
            updated_at=utc_now(),
        )
        raise _error(409, str(error)) from error
    if isinstance(error, RuntimeActivationCleanupPending):
        _record_pending_cleanup(service, intent, error)
    try:
        publication_committed = store.published_identity_matches(intent.commit_sha, intent.tag_name)
    except AgentGitError:
        publication_committed = True
    cleanup_error: Exception | None = None
    if binding is not None and not publication_committed:
        try:
            await service.release_activation_compensator(binding)
        except (RuntimeStoreError, RuntimeUpstreamError) as exc:
            cleanup_error = exc
    if cleanup_error is not None:
        _record_pending_cleanup(service, intent, cleanup_error)
    cancelled = reconcile_publication_failure(
        service.feedback_store.Session,
        store,
        intent=intent,
        detail=str(error),
        updated_at=utc_now(),
        add_event=service._add_event_row,
    )
    suffix = "; publication intent was cancelled before side effects" if cancelled else ""
    raise _error(409, f"Agent publish failed during release activation; previous active version was retained: {error}{suffix}") from error


def _record_pending_cleanup(service: _GovernanceService, intent: Any, error: Exception) -> None:
    record_publication_error(
        service.feedback_store.Session,
        change_set_id=intent.change_set_id,
        detail=str(error),
        updated_at=utc_now(),
    )
    raise _error(
        409,
        "Agent release activation cleanup is pending; previous active version was retained; retry the same publish command",
    ) from error


async def _commit_activation_metadata(service: _GovernanceService, intent: Any, binding: Any) -> None:
    try:
        await service.release_activation_committer(binding)
    except (RuntimeStoreError, RuntimeUpstreamError) as exc:
        record_publication_error(
            service.feedback_store.Session,
            change_set_id=intent.change_set_id,
            detail=str(exc),
            updated_at=utc_now(),
        )
        raise _error(
            409,
            "Agent Git activation completed, but Runtime activation metadata is pending reconciliation; retry publish",
        ) from exc


def _finalize_activation(
    service: _GovernanceService,
    change_set_id: str,
    intent: Any,
    binding: Any,
    result: JsonObject,
    *,
    assert_maintenance_active: Callable[[], None],
) -> JsonObject:
    archive = dict(result.get("archive")) if isinstance(result.get("archive"), dict) else {}
    archive.update(
        {
            "runtime_agent_id": binding.runtime_agent_id,
            "harness_digest": binding.harness_digest,
            "workspace_id": binding.workspace_id,
        }
    )
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


def _error(status_code: int, detail: str) -> Exception:
    from app.services.agent_governance import AgentGovernanceError

    return AgentGovernanceError(status_code, detail)
