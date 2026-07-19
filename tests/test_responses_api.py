"""POST /v1/responses 非流式：strict/control 双模式、agent_id 必填、instructions strict 处置、
previous_response_id 归属校验、请求映射与响应投影、hostile 输入。"""

from __future__ import annotations

from pathlib import Path

from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import ChatResponse
from app.runtime.session_store import LocalSession
from fastapi.testclient import TestClient

from test_agent_workspace_packages import _import_new_agent
from test_api_execution_optimizer import _load_app


def _register_biz(
    client: TestClient,
    agent_id: str = "soc-ops",
    name: str = "客服助手",
) -> None:
    assert _import_new_agent(client, agent_id=agent_id, name=name).status_code == 200


def _fake_capturing_run(captured: dict):
    async def fake_run(req, *, profile=None, **kwargs):
        captured["req"] = req
        captured["profile"] = profile
        return ChatResponse(
            run_id="run-123",
            session_id="sess-abc",
            sdk_session_id="sdk-1",
            agent_version_id="ver-1",
            answer="日报正文",
            usage={"input_tokens": 3, "output_tokens": 5},
            total_cost_usd=0.01,
            stop_reason="end_turn",
        )

    return fake_run


# ---------------------------------------------------------------- control 模式


def test_control_runs_business_agent_and_maps_fields(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post(
            "/v1/responses",
            json={
                "model": "claude-sonnet-5",
                "input": "帮我生成一份日报",
                "instructions": "只输出正文",
                "metadata": {"source": "playground"},
                "agentgov": {"agent_id": "soc-ops", "alert_id": "alert-1", "case_id": "case-1", "max_turns": 8},
            },
        )
        assert resp.status_code == 200, resp.text
    req = captured["req"]
    assert req.message == "帮我生成一份日报"
    assert req.alert_id == "alert-1" and req.case_id == "case-1"  # backend-owned，来自 agentgov
    assert req.max_turns == 8
    assert req.system_append == "只输出正文"  # control 下 instructions -> append-only system_append
    assert req.metadata.get("source") == "playground"
    assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")


def test_control_response_projection_shape(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        _register_biz(client)
        body = client.post(
            "/v1/responses",
            json={"input": "hi", "agentgov": {"agent_id": "soc-ops"}},
        ).json()
    assert body["id"] == "resp_run-123"
    assert body["object"] == "response"
    assert body["status"] == "completed"
    # 权威输出在 output[]
    assert body["output"][0]["content"][0]["text"] == "日报正文"
    ag = body["agentgov"]
    assert ag["agent_id"] == "soc-ops"
    assert ag["run_id"] == "run-123"
    assert ag["conversation_id"] == "conv_sess-abc"
    assert ag["output_text"] == "日报正文"  # 便利聚合在 agentgov 命名空间
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}


def test_control_input_items_store_false_and_reserved_metadata(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post(
            "/v1/responses",
            json={
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "第一段"}, {"text": "第二段"}]},
                    {"text": "第三段"},
                ],
                "store": False,
                "metadata": {"source": "playground", "__agentgov_store__": True},
                "agentgov": {"agent_id": "soc-ops"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

    assert captured["req"].message == "第一段\n第二段\n第三段"
    assert captured["req"].metadata == {"source": "playground", "__agentgov_store__": False}
    assert body["metadata"] == {"source": "playground"}


def test_control_missing_agent_id_422(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        # agentgov 存在但 agent_id 缺失 -> 硬 422，不静默跑 main
        assert client.post("/v1/responses", json={"input": "hi", "agentgov": {}}).status_code == 422
        assert client.post("/v1/responses", json={"input": "hi", "agentgov": {"agent_id": "  "}}).status_code == 422
        # HITL 由 workspace 原生 permissions.ask 与服务端交互面控制，不接受无效的请求级旁路字段。
        assert (
            client.post(
                "/v1/responses",
                json={"input": "hi", "agentgov": {"agent_id": "soc-ops", "hitl": {"enabled": True}}},
            ).status_code
            == 422
        )


def test_control_unknown_agent_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        assert client.post("/v1/responses", json={"input": "hi", "agentgov": {"agent_id": "ghost"}}).status_code == 404


# ---------------------------------------------------------------- 已退役控制字段


def test_retired_response_disposition_fields_are_rejected_and_absent_from_openapi(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    retired_fields = {
        "phase": "proposal",
        "approval_request_id": "approval-1",
        "playbook_digest": "a" * 64,
        "execution_run_id": "execution-1",
    }

    with TestClient(module.app) as client:
        for field, value in retired_fields.items():
            response = client.post(
                "/v1/responses",
                json={"input": "hi", "agentgov": {"agent_id": "main-agent", field: value}},
            )
            assert response.status_code == 422, (field, response.text)
        openapi = client.get("/openapi.json").json()

    schemas = openapi["components"]["schemas"]
    request_properties = schemas["AgentGovRequestExtension"]["properties"]
    response_properties = schemas["AgentGovResponseExtension"]["properties"]
    for field in retired_fields:
        assert field not in request_properties
        assert field not in response_properties


# ---------------------------------------------------------------- strict 模式


def test_strict_uses_operator_agent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        _register_biz(client)
        client.put("/api/settings/openai-compat-agent", json={"agent_id": "soc-ops"})
        assert client.post("/v1/responses", json={"input": "hi"}).status_code == 200
    assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")


def test_strict_unconfigured_runs_platform_default(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        assert client.post("/v1/responses", json={"input": "hi"}).status_code == 200
    assert captured["profile"] is not None and captured["profile"].agent_id == DEFAULT_BUSINESS_AGENT_ID


def test_strict_explicit_main_is_configured_but_runs_main(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        assert client.put("/api/settings/openai-compat-agent", json={"agent_id": "main-agent"}).json()["configured"] is True
        assert client.post("/v1/responses", json={"input": "hi"}).status_code == 200
    # 显式配置普通历史 Agent 与未配置的平台默认状态不同。
    assert captured["profile"] is not None and captured["profile"].agent_id == "main-agent"


def test_strict_rejects_instructions_422(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        # strict 不静默按 append 生效 instructions（append-only 非官方 replace）
        assert client.post("/v1/responses", json={"input": "hi", "instructions": "you are pirate"}).status_code == 422


def test_strict_fail_loud_503_when_operator_agent_deleted(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        _register_biz(client)
        client.put("/api/settings/openai-compat-agent", json={"agent_id": "soc-ops"})
        assert client.delete("/api/agent-registry/soc-ops").status_code == 200
        assert client.post("/v1/responses", json={"input": "hi"}).status_code == 503


# ---------------------------------------------------------------- 会话续接（流式见 test_responses_stream.py）


def test_previous_response_id_not_found_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post(
            "/v1/responses",
            json={"input": "hi", "previous_response_id": "resp_ghost", "agentgov": {"agent_id": "soc-ops"}},
        )
        assert resp.status_code == 404


def test_previous_response_id_conflict_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    module.feedback_store.record_run({"run_id": "prev1", "session_id": "sessA", "agent_id": "soc-ops"})
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post(
            "/v1/responses",
            json={
                "input": "hi",
                "previous_response_id": "resp_prev1",
                "conversation": "conv_sessB",  # 与 prev1 的 sessA 不一致
                "agentgov": {"agent_id": "soc-ops"},
            },
        )
        assert resp.status_code == 409


def test_previous_response_id_rejects_cross_agent_owner_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    module.feedback_store.record_run({"run_id": "prev-owned", "session_id": "sess-owned", "agent_id": "soc-ops"})
    with TestClient(module.app) as client:
        _register_biz(client)
        _register_biz(client, agent_id="other-agent", name="其他助手")
        response = client.post(
            "/v1/responses",
            json={"input": "hi", "previous_response_id": "resp_prev-owned", "agentgov": {"agent_id": "other-agent"}},
        )

    assert response.status_code == 409
    assert "different business agent" in response.json()["detail"]
    assert captured == {}


def test_previous_response_id_without_session_fails_closed_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    module.feedback_store.record_run({"run_id": "prev-no-session", "agent_id": "soc-ops"})
    with TestClient(module.app) as client:
        _register_biz(client)
        response = client.post(
            "/v1/responses",
            json={"input": "hi", "previous_response_id": "resp_prev-no-session", "agentgov": {"agent_id": "soc-ops"}},
        )

    assert response.status_code == 409
    assert "no resumable conversation" in response.json()["detail"]
    assert captured == {}


def test_previous_response_id_with_deleted_session_mapping_fails_closed_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    module.feedback_store.record_run({"run_id": "prev-deleted", "session_id": "sess-deleted", "agent_id": "soc-ops"})
    with TestClient(module.app) as client:
        _register_biz(client)
        response = client.post(
            "/v1/responses",
            json={"input": "hi", "previous_response_id": "resp_prev-deleted", "agentgov": {"agent_id": "soc-ops"}},
        )

    assert response.status_code == 409
    assert "mapping no longer exists" in response.json()["detail"]
    assert captured == {}


def test_conversation_rejects_cross_agent_session_owner_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    module.session_store.save(LocalSession(session_id="sess-owned", agent_id="soc-ops", sdk_session_id="sdk-owned"))
    with TestClient(module.app) as client:
        _register_biz(client)
        _register_biz(client, agent_id="other-agent", name="其他助手")
        response = client.post(
            "/v1/responses",
            json={"input": "hi", "conversation": "conv_sess-owned", "agentgov": {"agent_id": "other-agent"}},
        )

    assert response.status_code == 409
    assert "different business agent" in response.json()["detail"]
    assert captured == {}


def test_conversation_rejects_historical_session_without_owner_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    module.session_store.save(LocalSession(session_id="sess-legacy", agent_id=None, sdk_session_id="sdk-legacy", turns=1))
    with TestClient(module.app) as client:
        _register_biz(client)
        response = client.post(
            "/v1/responses",
            json={"input": "hi", "conversation": "conv_sess-legacy", "agentgov": {"agent_id": "soc-ops"}},
        )

    assert response.status_code == 409
    assert "no unambiguous business agent owner" in response.json()["detail"]
    assert captured == {}


# ---------------------------------------------------------------- hostile 输入


def test_agentgov_unknown_field_rejected_422(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        _register_biz(client)
        # agentgov extra=forbid：未知字段被拒
        resp = client.post(
            "/v1/responses",
            json={"input": "hi", "agentgov": {"agent_id": "soc-ops", "updated_input": {"x": 1}}},
        )
        assert resp.status_code == 422


def test_previous_response_id_continues_session(monkeypatch, tmp_path: Path) -> None:
    # 正向续接：previous_response_id -> 解析原 run 的 session -> 透传 ChatRequest.session_id
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    module.feedback_store.record_run({"run_id": "prevA", "session_id": "sessA", "agent_id": "soc-ops"})
    module.session_store.get_or_create_owned("sessA", agent_id="soc-ops")
    with TestClient(module.app) as client:
        _register_biz(client)
        assert client.post("/v1/responses", json={"input": "hi", "previous_response_id": "resp_prevA", "agentgov": {"agent_id": "soc-ops"}}).status_code == 200
    assert captured["req"].session_id == "sessA"


def test_conversation_continues_session(monkeypatch, tmp_path: Path) -> None:
    # 正向续接：conversation=conv_<sid> -> session_id 透传
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        _register_biz(client)
        assert client.post("/v1/responses", json={"input": "hi", "conversation": "conv_sessB", "agentgov": {"agent_id": "soc-ops"}}).status_code == 200
    assert captured["req"].session_id == "sessB"


def test_client_cannot_inject_reserved_store_marker(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        _register_biz(client)
        # 客户端在 metadata 里塞保留 backend key（想伪造 store 标记）—— 请求侧与响应回显都应剥离
        body = client.post(
            "/v1/responses",
            json={"input": "hi", "metadata": {"__agentgov_store__": False}, "agentgov": {"agent_id": "soc-ops"}},
        ).json()
    assert "__agentgov_store__" not in captured["req"].metadata
    assert "__agentgov_store__" not in body["metadata"]
