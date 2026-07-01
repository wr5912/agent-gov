from fastapi.testclient import TestClient

from app.runtime.errors import BusinessRuleViolation
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
