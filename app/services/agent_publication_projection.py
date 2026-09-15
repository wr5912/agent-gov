from __future__ import annotations

from collections.abc import Callable

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel
from app.services.agent_governance_projections import matching_passed_test_run, matching_recorded_passed_test_run, projected_publication_blocker
from app.services.agent_publication import PublicationIntent


def project_change_set_publication_state(
    row: AgentChangeSetModel,
    change_set: JsonObject,
    *,
    test_run_by_id: Callable[[str], JsonObject | None] | None,
    latest_candidate_test_run: Callable[..., JsonObject | None] | None,
) -> JsonObject:
    """投影公开发布恢复证据及其精确绑定的测试记录。"""

    candidate = str(row.candidate_commit_sha or "")
    agent_id = str(change_set.get("agent_id") or "")
    publication_evidence = _publication_evidence(row, change_set, candidate=candidate, agent_id=agent_id)
    change_set["publication_evidence"] = publication_evidence
    test_run_id = _bound_test_run_id(row, change_set, publication_evidence)
    if test_run_id:
        candidate_run = test_run_by_id(test_run_id) if test_run_by_id else None
    else:
        candidate_run = (
            latest_candidate_test_run(
                agent_id=agent_id,
                commit_sha=candidate,
                change_set_id=str(row.change_set_id),
            )
            if latest_candidate_test_run and candidate
            else None
        )
    matcher = matching_recorded_passed_test_run if row.status == "published" else matching_passed_test_run
    passed_run = matcher(
        candidate_run,
        agent_id=agent_id,
        commit_sha=candidate,
        change_set_id=str(row.change_set_id),
        not_before=str(change_set.get("evidence_not_before") or "") or None,
    )
    if row.status == "published" and (
        publication_evidence is None
        or passed_run is None
        or passed_run["test_run_id"] != publication_evidence["test_run_id"]
        or passed_run["suite_digest"] != publication_evidence["suite_digest"]
    ):
        passed_run = None
    change_set["latest_test_run_id"] = passed_run.get("test_run_id") if passed_run else None
    change_set["latest_test_run"] = passed_run
    change_set["publication_blocker"] = projected_publication_blocker(change_set, passed_run)
    return change_set


def _publication_evidence(
    row: AgentChangeSetModel,
    change_set: JsonObject,
    *,
    candidate: str,
    agent_id: str,
) -> JsonObject | None:
    publication_intent = change_set.get("publication_intent")
    if row.status not in {"publishing", "published"} or not isinstance(publication_intent, dict):
        return None
    try:
        intent = PublicationIntent.from_payload(publication_intent)
    except ValueError:
        return None
    if (
        intent.change_set_id,
        intent.agent_id,
        intent.commit_sha,
        intent.previous_commit_sha,
    ) != (row.change_set_id, agent_id, candidate, row.base_commit_sha):
        return None
    return {
        "candidate_commit_sha": intent.commit_sha,
        "diff_digest": intent.diff_digest,
        "test_run_id": intent.test_run_id,
        "suite_digest": intent.suite_digest,
        "tag_name": intent.tag_name,
        "force": intent.force,
    }


def _bound_test_run_id(
    row: AgentChangeSetModel,
    change_set: JsonObject,
    publication_evidence: JsonObject | None,
) -> str:
    evidence: object = None
    if row.status == "approved":
        evidence = change_set.get("approval_evidence")
    elif publication_evidence is not None:
        evidence = publication_evidence
    return str(evidence.get("test_run_id") or "") if isinstance(evidence, dict) else ""
