from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel, AgentReleaseModel, utc_now
from app.runtime.state_machines import validate_transition
from app.services.agent_candidate_approval import CandidateApprovalFailure
from app.services.agent_governance_errors import AgentGovernanceError
from app.services.agent_governance_projections import publication_blocker_for_change_set
from app.services.agent_publication import (
    PublicationIntent,
    PublicationReservationLost,
    PublicationSourceConflict,
    PublicationTagConflict,
    capture_publication_source,
    commit_publication_intent,
    validate_intent_provenance,
)
from app.services.agent_publication_provenance import PublicationSourceRevision
from app.services.agent_publication_validation import (
    PublicationCandidate,
    publication_intent_after_reservation_race,
    require_existing_publication_request,
    validate_publication_candidate,
)


class PublicationReservationHost(Protocol):
    feedback_store: Any

    def _change_set_to_payload(self, row: AgentChangeSetModel) -> JsonObject: ...

    def _normalize_agent_id(self, agent_id: str | None) -> str: ...

    def _release_row_for_change_set(self, db: object, change_set_id: str) -> AgentReleaseModel | None: ...

    def _validate_publication_intent(
        self,
        row: AgentChangeSetModel,
        intent: PublicationIntent,
        *,
        requested_tag_name: str | None,
        db: Session | None = None,
    ) -> None: ...

    def _validate_publication_start(
        self,
        status: str,
        *,
        publication_blocker: str | None,
        force: bool,
        feedback_managed: bool,
    ) -> None: ...

    def _add_event_row(self, *args: Any, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class PublicationRequest:
    change_set_id: str
    operator: str
    tag_name: str | None
    note: str | None
    force: bool
    expected_candidate_commit_sha: str
    expected_diff_digest: str
    expected_test_run_id: str | None
    expected_suite_digest: str | None


@dataclass(frozen=True)
class _PreparedReservation:
    intent: PublicationIntent
    candidate: PublicationCandidate
    previous_status: str
    previous_updated_at: str
    before: JsonObject
    after: JsonObject


def reserve_publication_intent(
    host: PublicationReservationHost,
    request: PublicationRequest,
) -> PublicationIntent:
    with host.feedback_store.Session() as db:
        row = db.get(AgentChangeSetModel, request.change_set_id)
        if row is None:
            raise AgentGovernanceError(404, "Agent change set not found")
        change_set = host._change_set_to_payload(row)
        if change_set.get("legacy_publication_quarantine"):
            raise AgentGovernanceError(409, "Agent change set publication state is quarantined for operator review")
        existing = _existing_reservation(host, db, row, change_set, request)
        if existing is not None:
            return existing
        prepared = _prepare_new_reservation(host, db, row, change_set, request)
    return _commit_reservation(host, prepared, request)


def _existing_reservation(
    host: PublicationReservationHost,
    db: Session,
    row: AgentChangeSetModel,
    change_set: JsonObject,
    request: PublicationRequest,
) -> PublicationIntent | None:
    if row.status not in {"publishing", "published"} or not change_set.get("publication_intent"):
        return None
    intent = require_existing_publication_request(
        change_set,
        force=request.force,
        expected_candidate_commit_sha=request.expected_candidate_commit_sha,
        expected_diff_digest=request.expected_diff_digest,
        expected_test_run_id=request.expected_test_run_id,
        expected_suite_digest=request.expected_suite_digest,
    )
    assert intent is not None
    host._validate_publication_intent(row, intent, requested_tag_name=request.tag_name, db=db)
    validate_intent_provenance(db, intent)
    return intent


def _prepare_new_reservation(
    host: PublicationReservationHost,
    db: Session,
    row: AgentChangeSetModel,
    change_set: JsonObject,
    request: PublicationRequest,
) -> _PreparedReservation:
    source_revision = capture_publication_source(db, request.change_set_id)
    candidate = validate_publication_candidate(host, db, row, allow_missing_test=request.force)
    _require_current_candidate(request, candidate)
    blocker = publication_blocker_for_change_set(change_set)
    host._validate_publication_start(
        row.status,
        publication_blocker=blocker,
        force=request.force,
        feedback_managed=source_revision is not None,
    )
    if request.force and not (request.note or "").strip():
        raise AgentGovernanceError(422, "Force publication requires an explicit reason")
    if host._release_row_for_change_set(db, request.change_set_id) is not None:
        raise AgentGovernanceError(
            409,
            "Agent change set has release metadata without a current immutable publication intent; operator reconciliation is required",
        )
    intent = _new_intent(host, row, request, candidate, blocker, source_revision)
    validate_transition("agent_change_set", row.status, "publishing")
    before = dict(change_set)
    after = {
        **change_set,
        "status": "publishing",
        "updated_at": intent.started_at,
        "publication_intent": intent.to_payload(),
        "publication_error": None,
    }
    return _PreparedReservation(intent, candidate, row.status, row.updated_at, before, after)


def _require_current_candidate(request: PublicationRequest, candidate: PublicationCandidate) -> None:
    stale = (
        request.expected_candidate_commit_sha != candidate.commit_sha
        or request.expected_diff_digest != candidate.diff_digest
        or (not request.force and (request.expected_test_run_id != candidate.test_run_id or request.expected_suite_digest != candidate.suite_digest))
    )
    if stale:
        raise AgentGovernanceError(409, "The reviewed publication candidate or test evidence is stale")


def _new_intent(
    host: PublicationReservationHost,
    row: AgentChangeSetModel,
    request: PublicationRequest,
    candidate: PublicationCandidate,
    blocker: str | None,
    source_revision: PublicationSourceRevision | None,
) -> PublicationIntent:
    agent_id = host._normalize_agent_id(row.agent_id)
    return PublicationIntent(
        release_id=f"agr-{uuid.uuid5(uuid.NAMESPACE_URL, f'agentgov:{agent_id}:{request.change_set_id}')}",
        change_set_id=request.change_set_id,
        agent_id=agent_id,
        commit_sha=candidate.commit_sha,
        diff_digest=candidate.diff_digest,
        test_run_id=None if request.force else candidate.test_run_id,
        suite_digest=None if request.force else candidate.suite_digest,
        tag_name=request.tag_name or f"agent-release-{request.change_set_id}",
        operator=request.operator,
        note=request.note,
        force=request.force,
        force_publication_blocker=blocker if request.force else None,
        previous_status=row.status,
        started_at=utc_now(),
        previous_commit_sha=str(row.base_commit_sha),
        source_improvement_id=source_revision.improvement_id if source_revision else None,
        source_improvement_updated_at=source_revision.updated_at if source_revision else None,
    )


def _commit_reservation(
    host: PublicationReservationHost,
    prepared: _PreparedReservation,
    request: PublicationRequest,
) -> PublicationIntent:
    intent = prepared.intent
    try:
        commit_publication_intent(
            host.feedback_store.Session,
            intent=intent,
            previous_status=prepared.previous_status,
            previous_updated_at=prepared.previous_updated_at,
            before=prepared.before,
            after=prepared.after,
            add_event=host._add_event_row,
            validate_candidate_test=prepared.candidate.transaction_validator(
                agent_id=intent.agent_id,
                change_set_id=request.change_set_id,
            ),
        )
    except (CandidateApprovalFailure, PublicationSourceConflict, PublicationTagConflict) as exc:
        raise AgentGovernanceError(409, str(exc)) from exc
    except PublicationReservationLost:
        return publication_intent_after_reservation_race(
            host,
            request.change_set_id,
            requested_tag_name=request.tag_name,
            force=request.force,
            expected_candidate_commit_sha=request.expected_candidate_commit_sha,
            expected_diff_digest=request.expected_diff_digest,
            expected_test_run_id=request.expected_test_run_id,
            expected_suite_digest=request.expected_suite_digest,
        )
    return intent
