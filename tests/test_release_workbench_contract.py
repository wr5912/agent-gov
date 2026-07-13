from pathlib import Path

from app.runtime.response_schemas.agent_governance_response_schemas import AgentChangeSetEventResponse, AgentChangeSetResponse

ROOT = Path(__file__).resolve().parents[1]


def test_change_set_response_hides_publication_intent_and_types_error() -> None:
    response = AgentChangeSetResponse.model_validate(
        {
            "change_set_id": "agc-test",
            "agent_id": "main-agent",
            "created_at": "2026-07-10T00:00:00Z",
            "updated_at": "2026-07-10T00:01:00Z",
            "status": "publishing",
            "base_commit_sha": "base",
            "candidate_commit_sha": "candidate",
            "branch_name": "agent-change/agc-test",
            "worktree_path": "/runtime/worktrees/agc-test",
            "publication_error": {
                "detail": "release metadata is pending reconciliation",
                "updated_at": "2026-07-10T00:01:00Z",
            },
            "publication_intent": {"release_id": "internal-only", "operator": "private"},
        }
    ).model_dump(mode="json")

    assert "publication_intent" not in response
    assert response["publication_error"] == {
        "detail": "release metadata is pending reconciliation",
        "updated_at": "2026-07-10T00:01:00Z",
    }
    event = AgentChangeSetEventResponse.model_validate(
        {
            "event_id": "age-test",
            "change_set_id": "agc-test",
            "action": "publication_started",
            "operator": "tester",
            "created_at": "2026-07-10T00:00:00Z",
            "before": {},
            "after": {"status": "publishing", "publication_intent": {"release_id": "internal-only"}},
        }
    ).model_dump(mode="json")
    assert "publication_intent" not in event["after"]


def test_release_workbench_separates_publish_retry_and_force_actions() -> None:
    source = (ROOT / "frontend/src/components/ReleaseWorkbench.tsx").read_text(encoding="utf-8")

    assert 'data-testid="release-action-publish"' in source
    assert 'data-testid="release-action-retry"' in source
    assert 'data-testid="release-action-force"' in source
    assert 'const retryTarget = selectedChangeSet?.candidate_commit_sha && selectedChangeSet.status === "publishing" ? selectedChangeSet : null' in source
    assert "const readyTarget = selectedChangeSet?.candidate_commit_sha" in source
    assert "const forceTarget = selectedChangeSet?.candidate_commit_sha" in source
    assert "deriveGates(selectedChangeSet)" in source
    assert 'optional: "可选"' in source
    assert "CHANGESET_FORCEABLE.has(String(selectedChangeSet.status))" in source
    assert "CHANGESET_REGRESSION_RUNNABLE.has(String(selectedChangeSet.status))" in source
    assert "!selectedChangeSet.publication_blocker" in source
    assert "!selectedChangeSet.publication_provenance_blocker" in source
    assert "CHANGESET_BLOCKED.has(String(cs.status))) || pendingChangeSets.find" not in source
    assert "selectedChangeSet.publication_error?.detail" in source
    assert "confirmedForceTarget.change_set_id" in source
