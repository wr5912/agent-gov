from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.agent_testing.models import AgentTestRunItemModel, AgentTestRunModel
from app.runtime.json_types import JsonObject
from app.services.agent_governance_projections import (
    candidate_diff_digest,
    matching_passed_test_run,
    require_approval_evidence,
)

FileDiffLoader = Callable[[str], JsonObject | None]
CANDIDATE_EVIDENCE_EPOCH = "exact-candidate-review/v1"


class CandidateApprovalFailure(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CandidateApprovalHost(Protocol):
    test_run_by_id: Callable[[str], JsonObject | None] | None

    def get_change_set(self, change_set_id: str) -> JsonObject | None: ...

    def change_set_diff(self, change_set: JsonObject, candidate: str) -> JsonObject | None: ...

    def change_set_file_diff(
        self,
        change_set: JsonObject,
        candidate: str,
        path: str,
    ) -> JsonObject | None: ...

    def _transition_change_set(
        self,
        change_set_id: str,
        status: str,
        *,
        fields: JsonObject,
        action: str,
        operator: str,
        expected_fields: JsonObject | None = None,
        transaction_mutation: Callable[[Session], None] | None = None,
    ) -> JsonObject: ...


@dataclass(frozen=True)
class ReviewedFileEvidence:
    path: str
    detail_sha256: str

    def to_payload(self) -> JsonObject:
        return {"path": self.path, "detail_sha256": self.detail_sha256}


@dataclass(frozen=True)
class CandidateReviewEvidence:
    files: tuple[ReviewedFileEvidence, ...]
    review_digest: str

    def compact_payload(self) -> JsonObject:
        return {
            "review_digest": self.review_digest,
            "reviewed_file_count": len(self.files),
        }


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _changed_entries(diff: JsonObject) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for bucket, status in (("added", "added"), ("modified", "modified"), ("deleted", "deleted")):
        values = diff.get(bucket)
        if not isinstance(values, list):
            raise CandidateApprovalFailure(409, "Candidate diff is invalid for mandatory approval")
        for value in values:
            path = value.get("path") if isinstance(value, dict) else None
            if not isinstance(path, str) or not path or path in seen:
                raise CandidateApprovalFailure(409, "Candidate diff contains an invalid or duplicate path")
            seen.add(path)
            entries.append((path, status))
    if not entries:
        raise CandidateApprovalFailure(409, "Candidate has no reviewable file changes")
    return tuple(sorted(entries))


def _has_changed_line(unified_diff: str) -> bool:
    return any(line.startswith(("+", "-")) and not line.startswith(("+++", "---")) for line in unified_diff.splitlines())


def inspect_candidate_review(diff: JsonObject, load_file_diff: FileDiffLoader) -> CandidateReviewEvidence:
    from_version = str(diff.get("from_version_id") or "")
    to_version = str(diff.get("to_version_id") or "")
    if not from_version or not to_version:
        raise CandidateApprovalFailure(409, "Candidate diff has no exact base/candidate identity")
    files: list[ReviewedFileEvidence] = []
    for path, status in _changed_entries(diff):
        detail = load_file_diff(path)
        if (
            not isinstance(detail, dict)
            or detail.get("from_version_id") != from_version
            or detail.get("to_version_id") != to_version
            or detail.get("path") != path
            or detail.get("status") != status
            or detail.get("is_text") is not True
            or detail.get("truncated") is not False
            or not isinstance(detail.get("unified_diff"), str)
            or not _has_changed_line(str(detail["unified_diff"]))
        ):
            raise CandidateApprovalFailure(409, f"Candidate file diff is not completely reviewable: {path}")
        files.append(ReviewedFileEvidence(path=path, detail_sha256=_canonical_digest(detail)))
    payload = [item.to_payload() for item in files]
    return CandidateReviewEvidence(files=tuple(files), review_digest=_canonical_digest(payload))


def _require_review_claims(
    requested: Sequence[Mapping[str, object]],
    expected: CandidateReviewEvidence,
) -> None:
    claims: list[JsonObject] = []
    for item in requested:
        if set(item) != {"path", "detail_sha256"}:
            raise CandidateApprovalFailure(409, "Reviewed file evidence schema is invalid")
        path, digest = item.get("path"), item.get("detail_sha256")
        if not isinstance(path, str) or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CandidateApprovalFailure(409, "Reviewed file evidence identity is invalid")
        claims.append({"path": path, "detail_sha256": digest})
    expected_payload = [item.to_payload() for item in expected.files]
    if sorted(claims, key=lambda item: str(item["path"])) != expected_payload:
        raise CandidateApprovalFailure(409, "Reviewed file evidence no longer matches the complete candidate diff")


def _latest_test_run_validator(
    *,
    agent_id: str,
    commit_sha: str,
    change_set_id: str,
    test_run_id: str,
    suite_digest: str,
    not_before: str | None,
) -> Callable[[Session], None]:
    def validate(db: Session) -> None:
        require_exact_passed_candidate_test(
            db,
            agent_id=agent_id,
            commit_sha=commit_sha,
            change_set_id=change_set_id,
            test_run_id=test_run_id,
            suite_digest=suite_digest,
            require_latest=True,
            not_before=not_before,
        )

    return validate


def require_exact_passed_candidate_test(
    db: Session,
    *,
    agent_id: str,
    commit_sha: str,
    change_set_id: str,
    test_run_id: str | None = None,
    suite_digest: str | None = None,
    require_latest: bool,
    not_before: str | None = None,
) -> JsonObject:
    statement = select(AgentTestRunModel).where(
        AgentTestRunModel.agent_id == agent_id,
        AgentTestRunModel.commit_sha == commit_sha,
        AgentTestRunModel.change_set_id == change_set_id,
    )
    if not_before:
        statement = statement.where(AgentTestRunModel.created_at > not_before)
    if require_latest:
        statement = statement.order_by(
            AgentTestRunModel.created_at.desc(),
            AgentTestRunModel.test_run_id.desc(),
        ).limit(1)
    elif test_run_id:
        statement = statement.where(AgentTestRunModel.test_run_id == test_run_id)
    row = db.scalar(statement)
    if (
        row is None
        or (test_run_id is not None and row.test_run_id != test_run_id)
        or row.status != "passed"
        or not row.suite_digest
        or (suite_digest is not None and row.suite_digest != suite_digest)
    ):
        raise CandidateApprovalFailure(409, "The exact candidate test evidence is stale or not passed")
    has_item = db.scalar(select(exists(select(AgentTestRunItemModel.test_run_item_id).where(AgentTestRunItemModel.test_run_id == row.test_run_id))))
    has_nonpassing_item = db.scalar(
        select(
            exists(
                select(AgentTestRunItemModel.test_run_item_id).where(
                    AgentTestRunItemModel.test_run_id == row.test_run_id,
                    AgentTestRunItemModel.outcome != "passed",
                )
            )
        )
    )
    if not has_item or has_nonpassing_item:
        raise CandidateApprovalFailure(409, "The exact candidate test report is missing or contains non-passing items")
    return {
        "test_run_id": row.test_run_id,
        "agent_id": row.agent_id,
        "commit_sha": row.commit_sha,
        "change_set_id": row.change_set_id,
        "status": row.status,
        "suite_digest": row.suite_digest,
    }


def approve_candidate_change_set(
    host: CandidateApprovalHost,
    change_set_id: str,
    *,
    operator: str,
    note: str | None,
    candidate_commit_sha: str,
    diff_digest: str,
    test_run_id: str,
    suite_digest: str,
    reviewed_files: Sequence[Mapping[str, object]],
) -> JsonObject:
    change_set = host.get_change_set(change_set_id)
    if change_set is None:
        raise CandidateApprovalFailure(404, "Agent change set not found")
    candidate = str(change_set.get("candidate_commit_sha") or "")
    if not candidate or candidate != candidate_commit_sha:
        raise CandidateApprovalFailure(409, "Reviewed candidate commit no longer matches the change set")
    diff = host.change_set_diff(change_set, candidate)
    if diff is None or candidate_diff_digest(diff) != diff_digest:
        raise CandidateApprovalFailure(409, "Reviewed candidate diff no longer matches the change set")
    exact_run = matching_passed_test_run(
        host.test_run_by_id(test_run_id) if host.test_run_by_id is not None else None,
        agent_id=str(change_set["agent_id"]),
        commit_sha=candidate,
        change_set_id=change_set_id,
        not_before=str(change_set.get("evidence_not_before") or "") or None,
    )
    if exact_run is None:
        raise CandidateApprovalFailure(409, "候选审批前必须完成且通过精确 candidate commit 的平台测试。")
    if exact_run.get("suite_digest") != suite_digest:
        raise CandidateApprovalFailure(409, "Reviewed test suite digest no longer matches the test run")
    review = inspect_candidate_review(
        diff,
        lambda path: host.change_set_file_diff(change_set, candidate, path),
    )
    _require_review_claims(reviewed_files, review)
    try:
        evidence = require_approval_evidence(change_set, diff, exact_run)
    except ValueError as exc:
        raise CandidateApprovalFailure(409, str(exc)) from exc
    evidence.update(review.compact_payload())
    return host._transition_change_set(
        change_set_id,
        "approved",
        fields={
            "approval_note": note,
            "approval_evidence": evidence,
            "candidate_evidence_epoch": CANDIDATE_EVIDENCE_EPOCH,
        },
        action="approved",
        operator=operator,
        expected_fields={"candidate_commit_sha": candidate},
        transaction_mutation=_latest_test_run_validator(
            agent_id=str(change_set["agent_id"]),
            commit_sha=candidate,
            change_set_id=change_set_id,
            test_run_id=test_run_id,
            suite_digest=suite_digest,
            not_before=str(change_set.get("evidence_not_before") or "") or None,
        ),
    )


def approval_review_evidence_matches(value: object, review: CandidateReviewEvidence) -> bool:
    evidence = value if isinstance(value, dict) else {}
    return all(evidence.get(key) == expected for key, expected in review.compact_payload().items())
