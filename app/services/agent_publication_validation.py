from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel, AgentReleaseModel
from app.services.agent_candidate_approval import (
    CandidateApprovalFailure,
    approval_review_evidence_matches,
    inspect_candidate_review,
    require_exact_passed_candidate_test,
    require_recorded_publication_test,
)
from app.services.agent_governance_errors import AgentGovernanceError
from app.services.agent_governance_projections import (
    approval_evidence_matches,
    candidate_diff_digest,
    manual_approval_paths,
)
from app.services.agent_publication import (
    PublicationIntent,
    PublicationSourceConflict,
    PublicationTagConflict,
    release_matches_intent,
    validate_source_claim,
    validate_tag_claim,
)

PublicationArguments: TypeAlias = dict[str, object]


class PublicationValidationHost(Protocol):
    def _store_for(self, agent_id: str | None) -> GitAgentVersionStore: ...


class PublicationIntentHost(Protocol):
    feedback_store: Any

    def _change_set_to_payload(self, row: AgentChangeSetModel) -> JsonObject: ...

    def _validate_publication_intent(
        self,
        row: AgentChangeSetModel,
        intent: PublicationIntent,
        *,
        requested_tag_name: str | None,
        db: Session | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class PublicationCandidate:
    commit_sha: str
    diff_digest: str
    test_run_id: str | None
    suite_digest: str | None
    require_latest_test: bool
    evidence_not_before: str | None

    def transaction_validator(
        self,
        *,
        agent_id: str,
        change_set_id: str,
    ) -> Callable[[Session], None]:
        def validate(db: Session) -> None:
            if self.test_run_id is None or self.suite_digest is None:
                return
            require_exact_passed_candidate_test(
                db,
                agent_id=agent_id,
                commit_sha=self.commit_sha,
                change_set_id=change_set_id,
                test_run_id=self.test_run_id,
                suite_digest=self.suite_digest,
                require_latest=self.require_latest_test,
                not_before=self.evidence_not_before,
            )

        return validate


def validate_publication_candidate(
    host: PublicationValidationHost,
    db: Session,
    row: AgentChangeSetModel,
    *,
    allow_missing_test: bool = False,
) -> PublicationCandidate:
    candidate = str(row.candidate_commit_sha or "")
    if not candidate:
        raise AgentGovernanceError(409, "Agent change set has no candidate commit")
    store = host._store_for(row.agent_id)
    diff = store.diff_versions(str(row.base_commit_sha or ""), candidate)
    if diff is None:
        raise AgentGovernanceError(409, "Unable to inspect candidate paths for mandatory approval")
    try:
        sensitive_paths = manual_approval_paths(diff)
    except ValueError as exc:
        raise AgentGovernanceError(409, str(exc)) from exc
    if row.status != "approved" and sensitive_paths:
        raise AgentGovernanceError(
            409,
            "Agent instructions, skills, MCP, manifest, and subagent changes require explicit manual approval before publication",
        )
    approval = (row.payload_json or {}).get("approval_evidence")
    approval_payload = dict(approval) if isinstance(approval, dict) else {}
    require_latest = row.status != "approved"
    requested_test_run_id = str(approval_payload.get("test_run_id") or "") if not require_latest else None
    requested_suite_digest = str(approval_payload.get("suite_digest") or "") if not require_latest else None
    evidence_not_before = str((row.payload_json or {}).get("evidence_not_before") or "") or None
    passed_run: dict[str, object] | None
    try:
        passed_run = require_exact_passed_candidate_test(
            db,
            agent_id=str(row.agent_id),
            commit_sha=candidate,
            change_set_id=str(row.change_set_id),
            test_run_id=requested_test_run_id,
            suite_digest=requested_suite_digest,
            require_latest=require_latest,
            not_before=evidence_not_before,
        )
    except CandidateApprovalFailure as exc:
        if not allow_missing_test or row.status == "approved":
            raise AgentGovernanceError(
                409,
                "待发布版本缺少当前精确候选：commit_sha 完全匹配且完整通过的平台测试运行记录。",
            ) from exc
        passed_run = None
    if row.status == "approved":
        assert passed_run is not None
        try:
            review = inspect_candidate_review(
                diff,
                lambda path: store.diff_version_file(str(row.base_commit_sha or ""), candidate, path),
            )
        except CandidateApprovalFailure as exc:
            raise AgentGovernanceError(exc.status_code, exc.detail) from exc
        if not approval_evidence_matches(
            approval,
            diff=diff,
            candidate_commit_sha=candidate,
            passed_run=passed_run,
        ) or not approval_review_evidence_matches(approval, review):
            raise AgentGovernanceError(
                409,
                "Agent change set approval evidence no longer matches the candidate diff and test suite",
            )
    return PublicationCandidate(
        commit_sha=candidate,
        diff_digest=candidate_diff_digest(diff),
        test_run_id=str(passed_run["test_run_id"]) if passed_run else None,
        suite_digest=str(passed_run["suite_digest"]) if passed_run else None,
        require_latest_test=require_latest,
        evidence_not_before=evidence_not_before,
    )


def require_publication_intent_evidence(
    db: Session,
    *,
    row: AgentChangeSetModel,
    intent: PublicationIntent,
    store: GitAgentVersionStore,
) -> None:
    """未完成发布核准入；完成后核原发布事实，不回溯新版测试报告要求。"""

    candidate = str(row.candidate_commit_sha or "")
    base = str(row.base_commit_sha or "")
    if (intent.agent_id, intent.change_set_id, intent.commit_sha, intent.previous_commit_sha) != (row.agent_id, row.change_set_id, candidate, base):
        raise AgentGovernanceError(409, "Publication intent commit identity is stale")
    if row.status == "published":
        _require_completed_release_identity(db, row=row, intent=intent, store=store)
    diff = store.diff_versions(base, candidate)
    if diff is None or candidate_diff_digest(diff) != intent.diff_digest:
        raise AgentGovernanceError(409, "Publication intent diff evidence is stale")
    try:
        sensitive_paths = manual_approval_paths(diff)
    except ValueError as exc:
        raise AgentGovernanceError(409, str(exc)) from exc
    if sensitive_paths and intent.previous_status != "approved":
        raise AgentGovernanceError(409, "Publication intent lacks mandatory sensitive-path approval")

    payload = dict(row.payload_json or {})
    passed_run: JsonObject | None = None
    if not intent.force:
        try:
            passed_run = _require_publication_test(db, row=row, test_run_id=intent.test_run_id, suite_digest=intent.suite_digest)
        except CandidateApprovalFailure as exc:
            raise AgentGovernanceError(409, "Publication intent test evidence is stale") from exc
    _require_approval_intent_evidence(
        db,
        intent=intent,
        store=store,
        diff=diff,
        row=row,
        passed_run=passed_run,
        base=base,
        candidate=candidate,
    )
    if intent.force:
        if intent.source_improvement_id is not None:
            raise AgentGovernanceError(409, "Feedback-managed publication intent cannot bypass tests")
        if payload.get("publication_blocker") != intent.force_publication_blocker:
            raise AgentGovernanceError(409, "Force publication blocker evidence is stale")
    try:
        validate_tag_claim(db, intent)
        validate_source_claim(db, intent)
    except (PublicationTagConflict, PublicationSourceConflict) as exc:
        raise AgentGovernanceError(409, str(exc)) from exc


def _require_completed_release_identity(
    db: Session,
    *,
    row: AgentChangeSetModel,
    intent: PublicationIntent,
    store: GitAgentVersionStore,
) -> None:
    releases = db.scalars(select(AgentReleaseModel).where(AgentReleaseModel.change_set_id == row.change_set_id).limit(2)).all()
    if len(releases) != 1 or releases[0].rollback_of_release_id is not None or not release_matches_intent(releases[0], intent):
        raise AgentGovernanceError(409, "Completed publication release identity is inconsistent")
    try:
        git_matches = store.published_identity_matches(intent.commit_sha, intent.tag_name)
    except (AgentGitError, OSError, RuntimeError):
        git_matches = False
    if not git_matches:
        raise AgentGovernanceError(409, "Published Agent release no longer matches its live Git/tag identity")


def _require_publication_test(
    db: Session,
    *,
    row: AgentChangeSetModel,
    test_run_id: str | None,
    suite_digest: str | None,
) -> JsonObject:
    if row.status == "published":
        return require_recorded_publication_test(
            db,
            agent_id=row.agent_id,
            commit_sha=str(row.candidate_commit_sha or ""),
            change_set_id=row.change_set_id,
            test_run_id=test_run_id or "",
            suite_digest=suite_digest or "",
        )
    return require_exact_passed_candidate_test(
        db,
        agent_id=row.agent_id,
        commit_sha=str(row.candidate_commit_sha or ""),
        change_set_id=row.change_set_id,
        test_run_id=test_run_id,
        suite_digest=suite_digest,
        require_latest=False,
        not_before=str((row.payload_json or {}).get("evidence_not_before") or "") or None,
    )


def _require_approval_intent_evidence(
    db: Session,
    *,
    intent: PublicationIntent,
    store: GitAgentVersionStore,
    diff: JsonObject,
    row: AgentChangeSetModel,
    passed_run: JsonObject | None,
    base: str,
    candidate: str,
) -> None:
    if intent.previous_status != "approved":
        return
    change_set = dict(row.payload_json or {})
    approval = change_set.get("approval_evidence")
    approval_payload = dict(approval) if isinstance(approval, dict) else {}
    if intent.force:
        try:
            passed_run = _require_publication_test(
                db,
                row=row,
                test_run_id=str(approval_payload.get("test_run_id") or ""),
                suite_digest=str(approval_payload.get("suite_digest") or ""),
            )
        except CandidateApprovalFailure as exc:
            raise AgentGovernanceError(409, "Force publication approval test evidence is stale") from exc
    assert passed_run is not None
    try:
        review = inspect_candidate_review(
            diff,
            lambda path: store.diff_version_file(base, candidate, path),
        )
    except CandidateApprovalFailure as exc:
        raise AgentGovernanceError(exc.status_code, exc.detail) from exc
    evidence_matches = approval_evidence_matches(
        approval,
        diff=diff,
        candidate_commit_sha=candidate,
        passed_run=passed_run,
    ) and approval_review_evidence_matches(approval, review)
    if not evidence_matches:
        raise AgentGovernanceError(409, "Publication intent approval evidence is stale")


def complete_internal_publication_arguments(
    host: PublicationValidationHost,
    session_factory: sessionmaker,
    change_set_id: str,
    provided: PublicationArguments,
) -> PublicationArguments:
    required = {
        "expected_candidate_commit_sha",
        "expected_diff_digest",
        "expected_test_run_id",
        "expected_suite_digest",
    }
    if required.issubset(provided):
        return provided
    with session_factory() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        if row is None:
            raise AgentGovernanceError(404, "Agent change set not found")
        if row.status in {"publishing", "published"}:
            payload = dict(row.payload_json or {})
            if payload.get("legacy_publication_quarantine") or payload.get("legacy_publication_identity"):
                require_existing_publication_request(
                    {**payload, "status": row.status},
                    force=bool(provided.get("force")),
                    expected_candidate_commit_sha=str(provided.get("expected_candidate_commit_sha") or ""),
                    expected_diff_digest=str(provided.get("expected_diff_digest") or ""),
                    expected_test_run_id=(str(provided["expected_test_run_id"]) if provided.get("expected_test_run_id") else None),
                    expected_suite_digest=(str(provided["expected_suite_digest"]) if provided.get("expected_suite_digest") else None),
                )
            try:
                intent = PublicationIntent.from_payload(payload.get("publication_intent"))
            except ValueError as exc:
                raise AgentGovernanceError(409, "Agent change set has an invalid publication intent") from exc
            completed = {
                **provided,
                "expected_candidate_commit_sha": intent.commit_sha,
                "expected_diff_digest": intent.diff_digest,
            }
            if not intent.force:
                completed["expected_test_run_id"] = intent.test_run_id
                completed["expected_suite_digest"] = intent.suite_digest
            return completed
        current = validate_publication_candidate(
            host,
            db,
            row,
            allow_missing_test=bool(provided.get("force")),
        )
    completed = {
        **provided,
        "expected_candidate_commit_sha": current.commit_sha,
        "expected_diff_digest": current.diff_digest,
    }
    if not provided.get("force"):
        completed["expected_test_run_id"] = current.test_run_id
        completed["expected_suite_digest"] = current.suite_digest
    return completed


def require_publication_request_matches_intent(
    intent: PublicationIntent,
    *,
    force: bool,
    expected_candidate_commit_sha: str,
    expected_diff_digest: str,
    expected_test_run_id: str | None,
    expected_suite_digest: str | None,
) -> None:
    mismatch = (
        force != intent.force
        or expected_candidate_commit_sha != intent.commit_sha
        or expected_diff_digest != intent.diff_digest
        or (not force and (expected_test_run_id != intent.test_run_id or expected_suite_digest != intent.suite_digest))
    )
    if mismatch:
        raise AgentGovernanceError(409, "The publication request no longer matches its immutable intent")


def require_existing_publication_request(
    change_set: JsonObject,
    *,
    force: bool,
    expected_candidate_commit_sha: str,
    expected_diff_digest: str,
    expected_test_run_id: str | None,
    expected_suite_digest: str | None,
) -> PublicationIntent | None:
    if change_set.get("status") not in {"publishing", "published"}:
        return None
    if change_set.get("legacy_publication_quarantine"):
        raise AgentGovernanceError(
            409,
            "Legacy publication is quarantined because exact candidate evidence is unavailable; inspect the change set and release state",
        )
    if change_set.get("legacy_publication_identity"):
        raise AgentGovernanceError(
            409,
            "Legacy published state is read-only because its original exact test and review evidence is unavailable; fetch the existing release",
        )
    try:
        intent = PublicationIntent.from_payload(change_set.get("publication_intent"))
    except ValueError as exc:
        raise AgentGovernanceError(409, "Agent change set has an invalid publication intent") from exc
    expected_owner = (
        str(change_set.get("change_set_id") or ""),
        str(change_set.get("agent_id") or ""),
        str(change_set.get("candidate_commit_sha") or ""),
        str(change_set.get("base_commit_sha") or ""),
    )
    actual_owner = (
        intent.change_set_id,
        intent.agent_id,
        intent.commit_sha,
        str(intent.previous_commit_sha or ""),
    )
    if actual_owner != expected_owner:
        raise AgentGovernanceError(409, "The publication intent no longer matches its change set identity")
    require_publication_request_matches_intent(
        intent,
        force=force,
        expected_candidate_commit_sha=expected_candidate_commit_sha,
        expected_diff_digest=expected_diff_digest,
        expected_test_run_id=expected_test_run_id,
        expected_suite_digest=expected_suite_digest,
    )
    return intent


def publication_intent_after_reservation_race(
    host: PublicationIntentHost,
    change_set_id: str,
    *,
    requested_tag_name: str | None,
    force: bool,
    expected_candidate_commit_sha: str,
    expected_diff_digest: str,
    expected_test_run_id: str | None,
    expected_suite_digest: str | None,
) -> PublicationIntent:
    with host.feedback_store.Session() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        if row is None:
            raise AgentGovernanceError(404, "Agent change set not found")
        payload = host._change_set_to_payload(row)
        if row.status not in {"publishing", "published"} or not payload.get("publication_intent"):
            raise AgentGovernanceError(409, "Agent change set changed while publication was being reserved")
        intent = require_existing_publication_request(
            payload,
            force=force,
            expected_candidate_commit_sha=expected_candidate_commit_sha,
            expected_diff_digest=expected_diff_digest,
            expected_test_run_id=expected_test_run_id,
            expected_suite_digest=expected_suite_digest,
        )
        assert intent is not None
        host._validate_publication_intent(row, intent, requested_tag_name=requested_tag_name, db=db)
        return intent
