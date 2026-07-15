from pathlib import Path

import pytest
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetEventResponse,
    AgentChangeSetRegressionReviewRequest,
    AgentChangeSetResponse,
)
from pydantic import ValidationError

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
    assert 'data-testid="release-action-retry-cleanup"' in source
    assert 'const retryTarget = selectedChangeSet?.candidate_commit_sha && selectedChangeSet.status === "publishing" ? selectedChangeSet : null' in source
    assert "const readyTarget = selectedChangeSet?.candidate_commit_sha" in source
    assert "const forceTarget = isForcePublishTarget(selectedChangeSet)" in source
    assert "deriveGates(selectedChangeSet)" in source
    assert 'optional: "可选"' in source
    assert "CHANGESET_FORCEABLE.has(String(changeSet.status))" in source
    assert "CHANGESET_REGRESSION_RUNNABLE.has(String(selectedChangeSet.status))" in source
    assert "!selectedChangeSet.publication_blocker" in source
    assert "!changeSet.publication_provenance_blocker" in source
    assert "CHANGESET_BLOCKED.has(String(cs.status))) || pendingChangeSets.find" not in source
    assert "selectedChangeSet.publication_error?.detail" in source
    assert "confirmedForceTarget.change_set_id" in source
    assert "selectedChangeSet?.latest_eval_run" in source
    assert "changeSet.worktree_cleanup_pending" in source


def test_release_workbench_force_publish_uses_review_gate_not_raw_item_evidence() -> None:
    source = (ROOT / "frontend/src/components/ReleaseWorkbench.tsx").read_text(encoding="utf-8")

    assert 'changeSet?.latest_eval_run?.gate_result.status === "review_required"' in source
    assert "isForcePublishTarget(selectedChangeSet)" in source
    assert "isForcePublishTarget(cs)" in source
    assert 'some((item) => item.status === "needs_human_review")' not in source


def test_release_workbench_requires_complete_dedicated_regression_review() -> None:
    source = (ROOT / "frontend/src/components/ReleaseWorkbench.tsx").read_text(encoding="utf-8")
    runtime_api = (ROOT / "frontend/src/api/runtime.ts").read_text(encoding="utf-8")

    assert 'item.status === "needs_human_review"' in source
    assert "reviewItems.every((item) => Boolean(reviewDecisions[item.dataset_case_id]))" in source
    assert "Boolean(reviewOperator.trim())" in source
    assert "Boolean(reviewReason.trim())" in source
    assert "&& !hasPendingReview" in source
    assert 'data-testid="release-regression-review"' in source
    assert 'data-testid="release-review-submit"' in source
    assert "data-decision={decision}" in source
    assert "disabled={!reviewComplete || Boolean(busyAction)}" in source
    assert "hasUnresolvedRegressionReview(selectedChangeSet)" in source
    assert "reviewAgentChangeSetRegression(" in source
    assert "dataset_case_id: item.dataset_case_id" in source
    assert "decision: reviewDecisions[item.dataset_case_id]" in source
    assert "review_id: `review-${latestEvalRun.eval_run_id}`" in source
    assert 'scope: "current_eval_run"' in source
    assert "operator: reviewOperator.trim()" in source
    assert "reason: reviewReason.trim()" in source
    assert "decisions," in source
    assert 'changeSet?.latest_eval_run?.gate_result.status === "review_required"' in source
    assert "pendingReviewCaseIds.has(item.dataset_case_id)" in source
    assert "await onRefresh();" in source
    assert "export function reviewAgentChangeSetRegression(" in runtime_api
    assert "/regression-runs/${encodeURIComponent(evalRunId)}/review" in runtime_api
    assert "body: JSON.stringify(payload)" in runtime_api


def test_regression_review_request_forbids_missing_audit_and_backend_binding_fields() -> None:
    payload = {
        "review_id": "review-1",
        "operator": "reviewer",
        "reason": "已核验全部待复核用例",
        "scope": "current_eval_run",
        "decisions": [{"dataset_case_id": "tdc-1", "decision": "approve"}],
    }
    assert AgentChangeSetRegressionReviewRequest.model_validate(payload).model_dump(mode="json") == {
        **payload,
        "decisions": [{"dataset_case_id": "tdc-1", "decision": "approve", "note": ""}],
    }
    for field in ("review_id", "operator", "reason", "scope", "decisions"):
        with pytest.raises(ValidationError):
            AgentChangeSetRegressionReviewRequest.model_validate({key: value for key, value in payload.items() if key != field})
    with pytest.raises(ValidationError):
        AgentChangeSetRegressionReviewRequest.model_validate({**payload, "change_set_id": "client-owned-forbidden"})


def test_real_container_acceptance_retries_only_governor_generated_writable_plans() -> None:
    source = (ROOT / "scripts/improvement_ui_e2e/real_container_flow.mjs").read_text(encoding="utf-8")

    assert "const MAX_GOVERNOR_PLAN_ATTEMPTS = 3;" in source
    assert 'getByTestId("decision-regenerate-optimization-plan")' in source
    assert 'response.request().method() === "POST"' in source
    assert "/optimization-plan/generate`" in source
    assert "attempt <= MAX_GOVERNOR_PLAN_ATTEMPTS" in source
    assert "governor did not produce a writable execution plan" in source
    assert 'method: "PUT"' not in source


def test_real_container_acceptance_keeps_normal_publish_then_runs_real_rejected_force_chain() -> None:
    source = (ROOT / "scripts/improvement_ui_e2e/real_container_flow.mjs").read_text(encoding="utf-8")
    runtime_client = (ROOT / "scripts/improvement_ui_e2e/runtime_client.mjs").read_text(encoding="utf-8")
    entrypoint = source.split("export async function runRealContainerAcceptance", maxsplit=1)[1]

    assert entrypoint.index("reviewRegressionAndPublish(page, config, flow, regression)") < entrypoint.index(
        "runRejectedPublicationFlow(page, config)"
    )
    assert 'seedBaseImprovement(config, "pagination-integrity")' in source
    assert '"evidence-conflict": {' in runtime_client
    assert '"pagination-integrity": {' in runtime_client
    assert 'fixtureName = "evidence-conflict"' in runtime_client
    assert "不得静默选边" in runtime_client
    assert "next_cursor、has_more、truncated、partial" in runtime_client
    assert "additionalFeedbacks" in runtime_client
    assert "exerciseFourStageActions(page, config, seed, 2)" in source
    assert 'index === 0 ? "reject" : "approve"' in source
    assert 'blocked.status !== "regression_failed"' in source
    assert 'const REJECTED_CASE_BLOCKER = "（1 条用例经人工复核拒绝）";' in source
    assert 'request.force !== true' in source
    assert 'event.action === "force_published"' in source
    assert '"desktop-rejected-force-confirm"' in source
