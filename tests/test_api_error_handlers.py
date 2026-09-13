from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.runtime.published_harness_preparation import prepare_published_harnesses
from app.runtime_gateway.store import harness_digest
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID


def test_removed_live_workspace_and_release_head_write_routes_are_unreachable(
    process_environment,
    tmp_path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    removed_paths = (
        "/api/agent-repository/discard-changes",
        "/api/agent-repository/snapshot",
        "/api/agent-releases/release-id/restore",
        "/api/agent-releases/release-id/rollback",
    )

    with TestClient(module.app) as client:
        for path in removed_paths:
            response = client.post(path, json={})
            assert response.status_code == 404, (path, response.text)
        openapi_paths = client.get("/openapi.json").json()["paths"]

    assert all(path not in openapi_paths for path in removed_paths[:2])
    assert "/api/agent-releases/{release_id}/restore" not in openapi_paths
    assert "/api/agent-releases/{release_id}/rollback" not in openapi_paths


def test_feedback_store_error_handler_returns_structured_error(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path)

    with TestClient(module.app) as client:
        response = client.post("/api/feedback-signals", json={"labels": ["tool_data_incomplete"]})

    assert response.status_code == 400
    assert response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"
    assert "run_id, session_id, alert_id, or case_id" in response.json()["detail"]


def test_feedback_route_not_found_returns_structured_error(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path)

    with TestClient(module.app) as client:
        response = client.get("/api/feedback-cases/fbc-missing")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Feedback case not found",
        "error_code": "NOT_FOUND",
    }


def test_feedback_case_create_unknown_typed_source_returns_not_found(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path)

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


def test_feedback_route_conflict_returns_structured_error(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path)

    with TestClient(module.app) as client:
        created = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "非法跨段"})
        response = client.post(f"/api/improvements/{created.json()['improvement_id']}/lifecycle", json={"stage": "release"})

    assert response.status_code == 409
    assert response.json()["error_code"] == "STATE_TRANSITION_ERROR"
    assert "transition" in response.json()["detail"].lower()


def test_public_finite_value_inputs_reject_unknown_values_at_validation_boundary(
    process_environment,
    tmp_path,
):
    module = _load_app(process_environment, tmp_path)

    with TestClient(module.app) as client:
        invalid_queries = (
            ("/api/agent-change-sets", {"status": "unknown"}),
            ("/api/agent-releases", {"status": "unknown"}),
            ("/api/agent-test-runs/history", {"status": "unknown"}),
            ("/api/assets", {"asset_type": "unknown"}),
            ("/api/agent-jobs", {"job_type": "unknown"}),
            ("/api/agent-jobs", {"status": "unknown"}),
            ("/api/feedback-cases", {"status": "unknown"}),
            ("/api/feedback-signals", {"source_type": "unknown"}),
            ("/api/soc-events", {"event_type": "unknown"}),
            ("/api/pending-correlations", {"status": "unknown"}),
        )
        for path, params in invalid_queries:
            response = client.get(path, params=params)
            assert response.status_code == 422, (path, response.text)

        invalid_bodies = (
            ("/api/agent-registry/unknown/lifecycle", {"status": "unknown"}),
            ("/api/improvements/unknown/lifecycle", {"stage": "unknown"}),
            ("/api/assets", {"agent_id": "soc", "asset_type": "unknown", "title": "x"}),
        )
        for path, body in invalid_bodies:
            response = client.post(path, json=body)
            assert response.status_code == 422, (path, response.text)

        for alias in ("feedback_signal", "event", "pending"):
            get_response = client.get(f"/api/feedback-sources/{alias}/unknown")
            patch_response = client.patch(f"/api/feedback-sources/{alias}/unknown", json={})
            assert get_response.status_code == 422, (alias, get_response.text)
            assert patch_response.status_code == 422, (alias, patch_response.text)


def test_agent_change_set_route_not_found_returns_structured_error(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path)

    with TestClient(module.app) as client:
        response = client.get("/api/agent-change-sets/agc-missing")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Agent change set not found",
        "error_code": "NOT_FOUND",
    }


def test_agent_change_set_publish_requires_explicit_review_fence(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path)

    with TestClient(module.app) as client:
        response = client.post("/api/agent-change-sets/agc-missing/publish", json={})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert {item["loc"][-1] for item in detail} >= {
        "expected_candidate_commit_sha",
        "expected_diff_digest",
    }


def test_agent_change_set_abandon_cleans_real_worktree_and_execution_claim(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path, extra_agent_ids=(ORDINARY_TEST_AGENT_ID,))
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

    with TestClient(module.app) as client:
        response = client.post(f"/api/agent-change-sets/{claim.change_set_id}/abandon", json={})
        repeated = client.post(f"/api/agent-change-sets/{claim.change_set_id}/abandon", json={})
        publish = client.post(
            f"/api/agent-change-sets/{claim.change_set_id}/publish",
            json={
                "expected_candidate_commit_sha": "a" * 40,
                "expected_diff_digest": "b" * 64,
                "expected_test_run_id": "atr-reviewed",
                "expected_suite_digest": "c" * 64,
            },
        )

    assert response.status_code == 200 and repeated.status_code == 200
    assert response.json()["status"] == "abandoned" and response.json()["worktree_cleanup_pending"] is False
    assert not worktree.exists()
    assert publish.status_code == 409
    actions = [event["action"] for event in module.agent_governance.list_change_set_events(claim.change_set_id)]
    assert actions.count("abandoned") == 1
    execution = module.improvement_content_store.get_execution(improvement.improvement_id)
    assert execution is not None and execution.status == "draft" and not execution.claim_token
    assert module.improvement_store.archive_improvement(improvement.improvement_id).improvement_status == "archived"


def test_chat_during_agent_version_maintenance_returns_structured_503(process_environment, tmp_path):
    process_environment.set("RUNTIME_CANDIDATES_DIR", str(tmp_path / "candidate-workspaces"))
    module = _load_app(
        process_environment,
        tmp_path,
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
        requires_web_hitl=False,
    )
    agent_store = module.agent_governance._store_for(ORDINARY_TEST_AGENT_ID)
    prepare_published_harnesses(module.settings)
    commit_sha = str(agent_store.current_commit_sha())
    version = agent_store.version_summary(commit_sha, reason="maintenance-boundary-test")
    agent_version_id = str(version["agent_version_id"])
    runtime_agent_id = "runtime-agent-maintenance"
    module.run_store.bind_session(
        session_id="session-maintenance",
        agent_id=ORDINARY_TEST_AGENT_ID,
        agent_version_id=agent_version_id,
        runtime_agent_id=runtime_agent_id,
        digest=harness_digest(agent_store.repository_dir),
        idempotency_key=None,
    )

    with module.agent_governance.version_maintenance.lease(
        agent_id=ORDINARY_TEST_AGENT_ID,
        kind="publish",
        owner_id="test",
    ):
        with TestClient(module.app) as client:
            response = client.post(
                "/api/runtime/chat/",
                json={
                    "agent_id": runtime_agent_id,
                    "session_id": "session-maintenance",
                    "client_operation_id": "maintenance-block",
                    "input": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                },
            )

    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": "Agent version maintenance or publish activation is in progress; retry after it completes.",
        "error_code": "RUNTIME_UNAVAILABLE",
    }


def test_api_key_authentication_returns_structured_401(process_environment, tmp_path):
    module = _load_app(process_environment, tmp_path, api_key="secret-token")

    with TestClient(module.app) as client:
        missing = client.get("/api/agent-registry")
        wrong_scheme = client.get("/api/agent-registry", headers={"Authorization": "Basic secret-token"})
        wrong_token = client.get("/api/agent-registry", headers={"Authorization": "Bearer wrong-token"})
        ok = client.get("/api/agent-registry", headers={"Authorization": "Bearer secret-token"})

    for response in (missing, wrong_scheme, wrong_token):
        assert response.status_code == 401
        assert response.json() == {
            "detail": "Invalid API key",
            "error_code": "UNAUTHORIZED",
        }
    assert ok.status_code == 200
