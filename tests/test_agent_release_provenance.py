"""现存 SQLite 记录的来源投影契约；不作为实际发布成功验收。"""

from __future__ import annotations

from app.runtime.improvement_db import ImprovementFeedbackCaseAssignmentModel
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import AgentReleaseModel, make_session_factory
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.services.agent_release_provenance import (
    released_versions_for_feedback_case,
    source_feedback_case_ids_by_release,
)
from fastapi.testclient import TestClient

from app_test_utils import load_test_app


def _assignment(case_id: str, improvement_id: str, agent_id: str) -> ImprovementFeedbackCaseAssignmentModel:
    return ImprovementFeedbackCaseAssignmentModel(
        feedback_case_id=case_id,
        improvement_id=improvement_id,
        feedback_id=f"feedback-{case_id}",
        agent_id=agent_id,
    )


def _release(release_id: str, source_id: str | None, agent_id: str, status: str = "published") -> AgentReleaseModel:
    return AgentReleaseModel(
        release_id=release_id,
        agent_id=agent_id,
        status=status,
        tag_name=f"tag-{release_id}",
        commit_sha=release_id.ljust(40, "a")[:40],
        change_set_id=f"change-{release_id}",
        payload_json={"source_improvement_id": source_id} if source_id else {},
    )


def test_provenance_requires_existing_same_agent_assignment_and_release(tmp_path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with session_factory.begin() as db:
        db.add_all(
            [
                _assignment("case-a", "improvement-a", "agent-a"),
                _assignment("case-b", "improvement-a", "agent-a"),
                _assignment("case-other", "improvement-a", "agent-b"),
                _release("release-a", "improvement-a", "agent-a"),
                _release("release-archived", "improvement-a", "agent-a", status="archived"),
                _release("release-other", "improvement-a", "agent-b"),
                _release("release-unrelated", "improvement-b", "agent-a"),
                _release("release-manual", None, "agent-a"),
            ]
        )

    forward = released_versions_for_feedback_case(session_factory, "case-a")
    assert {item.release_id for item in forward} == {"release-a", "release-archived"}
    assert {item.status for item in forward} == {"published", "archived"}
    assert released_versions_for_feedback_case(session_factory, "missing") == []

    releases = [
        {"release_id": "release-a", "agent_id": "agent-a", "source_improvement_id": "improvement-a"},
        {"release_id": "release-other", "agent_id": "agent-b", "source_improvement_id": "improvement-a"},
        {"release_id": "release-unrelated", "agent_id": "agent-a", "source_improvement_id": "improvement-b"},
        {"release_id": "release-manual", "agent_id": "agent-a", "source_improvement_id": None},
    ]
    reverse = source_feedback_case_ids_by_release(session_factory, releases)
    assert reverse.by_release_id == {
        "release-a": ["case-a", "case-b"],
        "release-other": ["case-other"],
        "release-unrelated": [],
        "release-manual": [],
    }


def test_public_api_projects_same_source_in_both_directions_without_new_release_write(process_environment, tmp_path) -> None:
    module = load_test_app(process_environment, tmp_path)
    signal = module.feedback_store.create_signal(FeedbackSignalCreateRequest(session_id="external-session-a"))
    module.feedback_store.reassign_signal_agent(signal["signal_id"], agent_id=DEFAULT_BUSINESS_AGENT_ID, operator="contract-test")
    case = module.feedback_store.create_case(source_refs=[("signal", signal["signal_id"])], title="来源投影契约")
    assert case is not None
    case_id = str(case["feedback_case_id"])
    improvement = module.improvement_store.create_improvement(agent_id=DEFAULT_BUSINESS_AGENT_ID, title="来源投影")
    module.improvement_content_store.attach_feedback_case(
        improvement.improvement_id,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        feedback_case_id=case_id,
        summary="来源投影契约",
    )
    with module.feedback_store.Session.begin() as db:
        db.add(_release("release-a", improvement.improvement_id, DEFAULT_BUSINESS_AGENT_ID))
    with TestClient(module.app) as client:
        forward = client.get(f"/api/asset-registry/feedback/{case_id}")
        reverse = client.get("/api/agent-releases/release-a")
        listed = client.get("/api/agent-releases")
    assert forward.status_code == reverse.status_code == listed.status_code == 200
    assert [item["release_id"] for item in forward.json()["released_versions"]] == ["release-a"]
    assert reverse.json()["source_feedback_case_ids"] == [case_id]
    assert listed.json()[0]["source_feedback_case_ids"] == [case_id]
