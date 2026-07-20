from pathlib import Path

from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetEventResponse,
    AgentChangeSetResponse,
)

from business_agent_test_utils import ORDINARY_TEST_AGENT_ID

ROOT = Path(__file__).resolve().parents[1]


def test_change_set_response_hides_publication_intent_and_types_error() -> None:
    response = AgentChangeSetResponse.model_validate(
        {
            "change_set_id": "agc-test",
            "agent_id": ORDINARY_TEST_AGENT_ID,
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


def test_release_workbench_uses_exact_commit_test_gate_and_separate_publish_actions() -> None:
    source = (ROOT / "frontend/src/components/ReleaseWorkbench.tsx").read_text(encoding="utf-8")

    assert 'data-testid="release-action-run-tests"' in source
    assert 'data-testid="release-action-cancel-tests"' in source
    assert 'data-testid="release-action-publish"' in source
    assert 'data-testid="release-action-retry"' in source
    assert 'data-testid="release-action-force"' not in source
    assert 'data-testid="release-action-retry-cleanup"' in source
    assert "latestExactRun(testRuns, selectedChangeSet?.candidate_commit_sha)" in source
    assert 'testRun.status === "passed"' in source
    assert "!selectedChangeSet.publication_blocker" in source
    assert "selectedChangeSet.publication_error?.detail" in source
    assert "selectedChangeSet?.latest_eval_run" not in source
    assert "reviewAgentChangeSetRegression" not in source


def test_feedback_workbench_displays_historical_force_warning_without_force_action() -> None:
    source = (ROOT / "frontend/src/components/ReleaseWorkbench.tsx").read_text(encoding="utf-8")
    runtime_api = "".join((ROOT / path).read_text(encoding="utf-8") for path in ("frontend/src/api/runtime.ts", "frontend/src/api/agentTesting.ts"))

    assert 'data-testid="release-action-force"' not in source
    assert 'data-testid="release-force-reason"' not in source
    assert "force: true" not in source
    assert "release.force_published" in source
    assert "测试条件被管理员绕过" in source
    assert "release.force_publication_blocker" in source
    assert "release.force_publish_reason" in source
    assert "release.operator" in source
    assert "reviewAgentChangeSetRegression" not in runtime_api
    assert "regression-runs" not in runtime_api


def test_real_container_acceptance_retries_only_governor_generated_writable_plans() -> None:
    source = (ROOT / "scripts/improvement_ui_e2e/real_container_flow.mjs").read_text(encoding="utf-8")
    runtime_client = (ROOT / "scripts/improvement_ui_e2e/runtime_client.mjs").read_text(encoding="utf-8")

    assert "const MAX_GOVERNOR_PLAN_ATTEMPTS = 3;" in source
    assert 'getByTestId("decision-regenerate-optimization-plan")' in source
    assert 'response.request().method() === "POST"' in source
    assert "/optimization-plan/generate" in source
    assert "attempt <= MAX_GOVERNOR_PLAN_ATTEMPTS" in source
    assert "governor did not produce a writable execution plan" in source
    assert 'method: "PUT"' not in source
    assert "config.testRunTimeoutMs" in source
    assert "Date.now() + config.actionTimeoutMs" not in source
    assert "REAL_TEST_RUN_TIMEOUT_MS || 900000" in runtime_client
    assert "authorizedTargetPaths" in runtime_client
    assert "requiredTestLiterals" in runtime_client
    assert "assertExecutionTargetScope(seed, execution)" in source
    assert "execution modified paths outside the confirmed feedback scope" in source
    assert "business Agent invocation evidence is incomplete or contains runtime errors" in source
