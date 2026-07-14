from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.runtime.agent_git_store import AgentGitError
from app.runtime.errors import BusinessRuleViolation, ConflictError
from app.runtime.improvement_db import ExecutionRecordModel
from app.runtime.schemas import EvalRunResponse
from app.runtime.test_dataset_schemas import TestCaseRecord as DatasetCaseRecord
from app.services.agent_change_set_provisioner import ChangeSetSource
from fastapi.testclient import TestClient

from feedback_store_test_utils import _seed_test_dataset
from test_api_execution_optimizer import _load_app


def test_feedback_store_error_handler_returns_structured_error(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        response = client.post("/api/feedback-signals", json={"labels": ["tool_data_incomplete"]})

    assert response.status_code == 400
    assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"
    assert "run_id, session_id, alert_id, or case_id" in response.json()["detail"]


def test_feedback_route_not_found_returns_structured_error(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        response = client.get("/api/feedback-cases/fbc-missing")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Feedback case not found",
        "error_code": "NOT_FOUND",
    }


def test_feedback_case_create_unknown_typed_source_returns_not_found(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        response = client.post(
            "/api/feedback-cases",
            json={"source_refs": [{"source_kind": "signal", "source_id": "sig-missing"}]},
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Feedback source not found",
        "error_code": "NOT_FOUND",
    }


def test_feedback_route_conflict_returns_structured_error(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        created = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "非法跨段"})
        response = client.post(f"/api/improvements/{created.json()['improvement_id']}/lifecycle", json={"stage": "release"})

    assert response.status_code == 409
    assert response.json()["error_code"] == "STATE_TRANSITION_ERROR"
    assert "transition" in response.json()["detail"].lower()


def test_feedback_workbench_preserves_domain_error_code(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    def create_improvement(**_kwargs):
        raise BusinessRuleViolation("domain-specific failure")

    monkeypatch.setattr(module.improvement_store, "create_improvement", create_improvement)

    with TestClient(module.app) as client:
        response = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "领域错误"})

    assert response.status_code == 400
    assert response.json() == {
        "detail": "domain-specific failure",
        "error_code": "BUSINESS_RULE_VIOLATION",
    }


def test_agent_change_set_route_not_found_returns_structured_error(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        response = client.get("/api/agent-change-sets/agc-missing")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Agent change set not found",
        "error_code": "NOT_FOUND",
    }


def test_agent_change_set_publish_conflict_returns_structured_error(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        response = client.post("/api/agent-change-sets/agc-missing/publish", json={})

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Agent change set not found",
        "error_code": "NOT_FOUND",
    }


def test_agent_change_set_regression_runtime_failure_is_retryable(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    improvement = module.improvement_store.create_improvement(agent_id="main-agent", title="回归异常恢复")
    change_set = module.agent_governance.create_change_set(
        title="回归异常恢复",
        execution_job_id="exec-retry",
        source=ChangeSetSource(improvement.improvement_id),
    )
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("CLAUDE.md").write_text("回归候选\n", encoding="utf-8")
    candidate = module.agent_version_store.commit_worktree(worktree, message="regression recovery candidate")
    change_set = module.agent_governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="exec-retry",
    )
    with module.feedback_store.Session.begin() as db:
        db.add(
            ExecutionRecordModel(
                execution_id="exec-retry",
                improvement_id=improvement.improvement_id,
                change_set_id=str(change_set["change_set_id"]),
                status="confirmed",
                applied_agent_version_id=candidate,
            )
        )
    _seed_test_dataset(
        module.feedback_store,
        agent_id="main-agent",
        dataset_id="tds-retry",
        candidate_agent_version_id=candidate,
        source_improvement_id=improvement.improvement_id,
        source_execution_id="exec-retry",
    )
    calls = 0
    completion_calls = 0

    async def flaky_regression(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("runtime exploded")
        run = module.feedback_store.create_eval_run(
            dataset_id="tds-retry",
            agent_version_id=candidate,
            source="agent_change_set_regression",
            change_set_id=str(change_set["change_set_id"]),
            regression_attempt_id=str(_kwargs["regression_attempt_id"]),
            candidate_commit_sha=candidate,
            candidate_worktree_path=str(worktree),
        )
        dataset_case = DatasetCaseRecord.model_validate(run["dataset_snapshot"]["cases"][0])
        module.feedback_store.append_eval_run_item(
            str(run["eval_run_id"]),
            dataset_case=dataset_case,
            agent_result={"run_id": "run-recovered", "agent_version_id": candidate, "answer": "ok"},
            status="passed",
            score=1.0,
            check_results=[],
        )
        finished = module.feedback_store.finish_eval_run(str(run["eval_run_id"]))
        assert finished is not None
        return EvalRunResponse.model_validate(finished)

    monkeypatch.setattr(module.runtime, "run_feedback_eval", flaky_regression)
    original_complete = module.agent_governance.complete_regression

    def flaky_completion(*args, **kwargs):
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise ConflictError("completion binding rejected")
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(module.agent_governance, "complete_regression", flaky_completion)
    path = f"/api/agent-change-sets/{change_set['change_set_id']}/regression-runs"
    with TestClient(module.app, raise_server_exceptions=False) as client:
        rejected = client.post(path, json={"dataset_id": "tds-retry", "eval_case_ids": ["evc-retry"]})
        failed = client.post(path, json={"dataset_id": "tds-retry"})
        failed_change_set = module.agent_governance.get_change_set(str(change_set["change_set_id"]))
        completion_failed = client.post(path, json={"dataset_id": "tds-retry"})
        completion_failed_change_set = module.agent_governance.get_change_set(str(change_set["change_set_id"]))
        recovered = client.post(path, json={"dataset_id": "tds-retry"})

    assert rejected.status_code == 422
    assert failed.status_code == 500
    assert failed_change_set["status"] == "regression_failed"
    assert failed_change_set["latest_eval_run_id"] is None
    assert failed_change_set["latest_eval_run"] is None
    assert failed_change_set["regression_error"]["error_type"] == "RuntimeError"
    assert completion_failed.status_code == 409
    assert completion_failed_change_set["status"] == "regression_failed"
    assert completion_failed_change_set["latest_eval_run_id"].startswith("evr-")
    assert completion_failed_change_set["latest_eval_run"]["status"] == "completed"
    assert completion_failed_change_set["regression_error"]["error_type"] == "ConflictError"
    assert recovered.status_code == 200
    assert recovered.json()["eval_run_id"].startswith("evr-")
    assert module.agent_governance.get_change_set(str(change_set["change_set_id"]))["status"] == "regression_passed"


def test_agent_change_set_abandon_cleans_worktree_and_cancels_execution_claim(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    improvement = module.improvement_store.create_improvement(agent_id="main-agent", title="执行取消")
    module.improvement_content_store.upsert_normalized_feedback(
        improvement.improvement_id,
        problem="p",
        advance_to_stage="triage",
    )
    module.improvement_content_store.upsert_attribution(
        improvement.improvement_id,
        summary="a",
        advance_to_stage="attribution",
    )
    module.improvement_content_store.set_attribution_status(improvement.improvement_id, status="confirmed")
    module.improvement_content_store.upsert_optimization_plan(
        improvement.improvement_id,
        summary="o",
        changes=[{"target": "prompt", "change": "x"}],
        advance_to_stage="optimization",
    )
    module.improvement_content_store.set_optimization_plan_status(improvement.improvement_id, status="confirmed")
    plan = module.improvement_content_store.get_optimization_plan(improvement.improvement_id)
    attribution = module.improvement_content_store.get_attribution(improvement.improvement_id)
    assert plan is not None and attribution is not None
    base = str(module.agent_version_store.current_commit_sha())
    claimed_at = datetime.now(UTC)
    claim = module.improvement_content_store.execution_claims.claim_execution(
        improvement.improvement_id,
        change_set_id="agc-11111111-2222-3333-4444-555555555555",
        base_commit_sha=base,
        source_optimization_plan_id=plan.optimization_plan_id,
        source_optimization_plan_updated_at=plan.updated_at,
        source_attribution_id=attribution.attribution_id,
        source_attribution_updated_at=attribution.updated_at,
        claim_token="claim-api-abandon",
        now=claimed_at.isoformat(),
        claim_expires_at=(claimed_at + timedelta(minutes=10)).isoformat(),
    )
    change_set = module.agent_governance.create_change_set(
        change_set_id=claim.change_set_id,
        base_commit_sha=base,
        execution_job_id=claim.execution_id,
    )
    worktree = Path(str(change_set["worktree_path"]))
    assert worktree.exists()
    agent_store = module.agent_governance._store_for("main-agent")
    remove_worktree = agent_store.remove_worktree
    cleanup_attempts = 0

    def fail_cleanup_once(change_set_id: str, *, delete_branch: bool = True) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise AgentGitError("cleanup interrupted")
        remove_worktree(change_set_id, delete_branch=delete_branch)

    monkeypatch.setattr(agent_store, "remove_worktree", fail_cleanup_once)

    with TestClient(module.app) as client:
        cleanup_failed = client.post(f"/api/agent-change-sets/{claim.change_set_id}/abandon", json={})

    interrupted = module.improvement_content_store.get_execution(improvement.improvement_id)
    pending_change_set = module.agent_governance.get_change_set(claim.change_set_id)
    assert cleanup_failed.status_code == 409
    assert interrupted is not None and interrupted.status == "draft" and not interrupted.claim_token
    assert pending_change_set is not None and pending_change_set["worktree_cleanup_pending"] is True

    replacement = module.improvement_content_store.execution_claims.claim_execution(
        improvement.improvement_id,
        change_set_id="agc-66666666-2222-3333-4444-555555555555",
        base_commit_sha=base,
        source_optimization_plan_id=plan.optimization_plan_id,
        source_optimization_plan_updated_at=plan.updated_at,
        source_attribution_id=attribution.attribution_id,
        source_attribution_updated_at=attribution.updated_at,
        claim_token="claim-immediate-retry",
        now=(claimed_at + timedelta(minutes=1)).isoformat(),
        claim_expires_at=(claimed_at + timedelta(minutes=11)).isoformat(),
    )
    module.improvement_content_store.execution_claims.finish_without_application(
        improvement.improvement_id,
        claim_token=replacement.claim_token,
        claim_generation=replacement.claim_generation,
        summary="retry claim acquired before old lease expired",
        retain_change_set=False,
    )

    with TestClient(module.app) as client:
        response = client.post(f"/api/agent-change-sets/{claim.change_set_id}/abandon", json={})
        repeated = client.post(f"/api/agent-change-sets/{claim.change_set_id}/abandon", json={})
        publish = client.post(f"/api/agent-change-sets/{claim.change_set_id}/publish", json={})

    assert response.status_code == 200 and repeated.status_code == 200
    assert response.json()["status"] == "abandoned" and response.json()["worktree_cleanup_pending"] is False
    assert not worktree.exists()
    assert publish.status_code == 409
    actions = [event["action"] for event in module.agent_governance.list_change_set_events(claim.change_set_id)]
    assert actions.count("abandoned") == 1
    execution = module.improvement_content_store.get_execution(improvement.improvement_id)
    assert execution is not None and execution.status == "draft" and not execution.claim_token
    assert module.improvement_store.archive_improvement(improvement.improvement_id).improvement_status == "archived"


def test_chat_during_agent_version_maintenance_returns_structured_503(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.agent_version_store, "is_maintenance_active", lambda: True)

    with TestClient(module.app) as client:
        # /api/chat 要求 agent_id 必填；用 main-agent 通过校验后才命中维护态 503。
        response = client.post("/api/chat", json={"message": "hello", "agent_id": "main-agent"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Agent version maintenance is in progress; retry after restore completes.",
        "error_code": "RUNTIME_UNAVAILABLE",
    }


def test_api_key_authentication_returns_structured_401(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path, api_key="secret-token")

    with TestClient(module.app) as client:
        missing = client.get("/api/agents")
        wrong_scheme = client.get("/api/agents", headers={"Authorization": "Basic secret-token"})
        wrong_token = client.get("/api/agents", headers={"Authorization": "Bearer wrong-token"})
        ok = client.get("/api/agents", headers={"Authorization": "Bearer secret-token"})

    for response in (missing, wrong_scheme, wrong_token):
        assert response.status_code == 401
        assert response.json() == {
            "detail": "Invalid API key",
            "error_code": "UNAUTHORIZED",
        }
    assert ok.status_code == 200
