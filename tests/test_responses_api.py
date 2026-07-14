"""POST /v1/responses 非流式：strict/control 双模式、agent_id 必填、instructions strict 处置、
previous_response_id 归属校验、请求映射与响应投影、hostile 输入。"""

from __future__ import annotations

from pathlib import Path

from app.runtime.schemas import ChatResponse
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _register_biz(
    client: TestClient,
    agent_id: str = "soc-ops",
    name: str = "客服助手",
    *,
    headers: dict[str, str] | None = None,
) -> None:
    assert (
        client.post(
            "/api/agent-registry",
            json={"name": name, "agent_id": agent_id},
            headers=headers,
        ).status_code
        == 201
    )


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


def test_control_unknown_agent_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run({}))
    with TestClient(module.app) as client:
        assert client.post("/v1/responses", json={"input": "hi", "agentgov": {"agent_id": "ghost"}}).status_code == 404


# ---------------------------------------------------------------- response-disposition trust boundary


def test_response_disposition_proposal_requires_ro_and_projects_trusted_context(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(
        monkeypatch,
        tmp_path,
        api_key="general-secret",
        response_orchestrator_api_key="ro-secret",
    )
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    general_headers = {"Authorization": "Bearer general-secret"}
    ro_headers = {"Authorization": "Bearer ro-secret"}
    request = {
        "input": "draft a response playbook",
        "agentgov": {
            "agent_id": "security-operations-expert",
            "case_id": "case-1",
            "phase": "proposal",
        },
    }

    with TestClient(module.app) as client:
        _register_biz(
            client,
            agent_id="security-operations-expert",
            name="Security Operations Expert",
            headers=general_headers,
        )
        assert client.post("/v1/responses", json=request, headers=general_headers).status_code == 403
        ordinary_ro = client.post(
            "/v1/responses",
            json={"input": "ordinary", "agentgov": {"agent_id": "security-operations-expert"}},
            headers=ro_headers,
        )
        response = client.post("/v1/responses", json=request, headers=ro_headers)

    assert ordinary_ro.status_code == 403
    assert response.status_code == 200, response.text
    disposition = captured["req"].response_disposition
    assert disposition is not None
    assert disposition.phase == "proposal"
    assert disposition.case_id == "case-1"
    assert response.json()["agentgov"]["phase"] == "proposal"


def test_response_disposition_approved_execution_validates_and_rejects_replay(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ENABLE_CLAUDE_WEB_HITL", "true")
    module = _load_app(
        monkeypatch,
        tmp_path,
        api_key="general-secret",
        response_orchestrator_api_key="ro-secret",
    )
    general_headers = {"Authorization": "Bearer general-secret"}
    ro_headers = {"Authorization": "Bearer ro-secret"}
    captured: dict = {}

    async def fake_stream(req, *, profile=None):
        captured["req"] = req
        yield {"event": "session", "data": {"run_id": "run-approved", "session_id": "session-approved"}}
        yield {"event": "done", "data": "[DONE]"}

    monkeypatch.setattr(module.runtime, "stream", fake_stream)
    request = {
        "input": "submit approved response playbook",
        "stream": True,
        "agentgov": {
            "agent_id": "security-operations-expert",
            "case_id": "case-1",
            "phase": "approved_execution",
            "approval_request_id": "approval-1",
            "playbook_digest": "a" * 64,
            "execution_run_id": "execution-1",
        },
    }

    with TestClient(module.app) as client:
        _register_biz(
            client,
            agent_id="security-operations-expert",
            name="Security Operations Expert",
            headers=general_headers,
        )
        assert client.post("/v1/responses", json=request, headers=general_headers).status_code == 403
        invalid_agent = {
            **request,
            "agentgov": {**request["agentgov"], "agent_id": "main-agent", "approval_request_id": "approval-other"},
        }
        assert client.post("/v1/responses", json=invalid_agent, headers=ro_headers).status_code == 422
        invalid_digest = {
            **request,
            "agentgov": {**request["agentgov"], "playbook_digest": "A" * 64},
        }
        assert client.post("/v1/responses", json=invalid_digest, headers=ro_headers).status_code == 422
        first = client.post("/v1/responses", json=request, headers=ro_headers)
        replay = client.post("/v1/responses", json=request, headers=ro_headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 409
    assert captured["req"].response_disposition.approval_request_id == "approval-1"
    claim = module.response_disposition_claim_store.get("approval-1")
    assert claim is not None
    assert claim.status == "cancelled"
    assert claim.agent_run_id == "run-approved"


def test_response_disposition_reports_missing_ro_configuration(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path, api_key="general-secret")
    general_headers = {"Authorization": "Bearer general-secret"}
    with TestClient(module.app) as client:
        _register_biz(
            client,
            agent_id="security-operations-expert",
            name="Security Operations Expert",
            headers=general_headers,
        )
        response = client.post(
            "/v1/responses",
            json={
                "input": "draft",
                "agentgov": {
                    "agent_id": "security-operations-expert",
                    "case_id": "case-1",
                    "phase": "proposal",
                },
            },
            headers=general_headers,
        )

    assert response.status_code == 503


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


def test_strict_unconfigured_runs_main(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        assert client.post("/v1/responses", json={"input": "hi"}).status_code == 200
    assert captured["profile"] is None  # 未配置 -> main


def test_strict_explicit_main_is_configured_but_runs_main(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        assert client.put("/api/settings/openai-compat-agent", json={"agent_id": "main-agent"}).json()["configured"] is True
        assert client.post("/v1/responses", json={"input": "hi"}).status_code == 200
    assert captured["profile"] is None  # 显式 main 与未配置状态不同，但运行目标仍是 main


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
