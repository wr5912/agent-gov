from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.runtime.agent_git_store import AgentGitError
from app.runtime.errors import BusinessRuleViolation
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID


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


def test_agent_change_set_abandon_cleans_worktree_and_cancels_execution_claim(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path, extra_agent_ids=(ORDINARY_TEST_AGENT_ID,))
    agent_store = module.agent_governance._store_for(ORDINARY_TEST_AGENT_ID)
    improvement = module.improvement_store.create_improvement(agent_id=ORDINARY_TEST_AGENT_ID, title="执行取消")
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
    base = str(agent_store.current_commit_sha())
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
        agent_id=ORDINARY_TEST_AGENT_ID,
    )
    worktree = Path(str(change_set["worktree_path"]))
    assert worktree.exists()
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
    module = _load_app(monkeypatch, tmp_path, extra_agent_ids=(ORDINARY_TEST_AGENT_ID,))
    # 维护态由 main.py 装配的 provider 判定，所有注册业务 Agent 走同一条校验路径。
    monkeypatch.setattr(module.runtime, "agent_version_maintenance_provider", lambda agent_id: True)

    with TestClient(module.app) as client:
        response = client.post("/api/chat", json={"message": "hello", "agent_id": ORDINARY_TEST_AGENT_ID})

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
