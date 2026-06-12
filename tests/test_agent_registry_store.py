"""AGV-004/022 基座：业务 Agent 身份注册表。

注册表只登记业务 Agent（被治理对象），治理 Agent（闭环执行者）不入表；
sync 幂等，作为运行/反馈/评估/版本治理的归属锚点。
"""

from __future__ import annotations

from pathlib import Path

from app.runtime.agent_profiles import build_profiles
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _store(tmp_path: Path) -> tuple[AgentRegistryStore, dict]:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    return AgentRegistryStore(factory), build_profiles(AppSettings())


def test_sync_registers_only_business_agents(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    store.sync_business_agents(profiles)

    agents = store.list_agents()
    assert [agent.agent_id for agent in agents] == ["main-agent"]
    assert agents[0].category == "business"
    # 治理 Agent 是闭环执行者，不作为被治理对象入注册表。
    assert store.get_agent("attribution-analyzer") is None


def test_sync_is_idempotent(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    store.sync_business_agents(profiles)
    store.sync_business_agents(profiles)  # 重复执行不得重复登记
    assert len(store.list_agents()) == 1


def test_get_agent_returns_stable_identity(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    store.sync_business_agents(profiles)

    record = store.get_agent("main-agent")
    assert record is not None
    assert record.name == "main-agent"
    assert record.workspace_dir  # 非空 workspace，作为归属锚点
    assert record.created_at


def test_lifespan_seeds_business_agent_registry(monkeypatch, tmp_path: Path) -> None:
    """应用启动（lifespan）幂等登记业务 Agent，使注册表在运行态被真实消费。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app):
        pass

    agents = module.agent_registry_store.list_agents()
    assert [agent.agent_id for agent in agents] == ["main-agent"]
    assert agents[0].category == "business"


def test_list_agents_endpoint_returns_registered_business_agents(monkeypatch, tmp_path: Path) -> None:
    """AGV-004/007：注册的业务 Agent 定义可经 API 查询，作为外部接入与归属对象的可见入口。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        response = client.get("/api/agent-registry")

    assert response.status_code == 200
    body = response.json()
    assert [item["agent_id"] for item in body] == ["main-agent"]
    assert body[0]["category"] == "business"
    assert body[0]["workspace_dir"]


def test_create_business_agent_endpoint_registers_and_lists(monkeypatch, tmp_path: Path) -> None:
    """AGV-004：可经 API 创建业务 Agent，获得稳定身份并进入注册表归属对象集合。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})
        listed = client.get("/api/agent-registry")
        duplicate = client.post("/api/agent-registry", json={"name": "重复", "agent_id": "soc-ops"})
        empty_name = client.post("/api/agent-registry", json={"name": "  "})

    assert created.status_code == 201
    assert created.json()["agent_id"] == "soc-ops"
    assert created.json()["category"] == "business"
    assert created.json()["workspace_dir"].endswith("/business-agents/soc-ops")
    assert {item["agent_id"] for item in listed.json()} == {"main-agent", "soc-ops"}
    assert duplicate.status_code == 409  # 重复身份被拒绝，不污染既有 Agent
    assert empty_name.status_code == 400  # 空名校验
    # 运行态前提：创建即初始化 workspace 与起始 CLAUDE.md。
    claude_md = Path(created.json()["workspace_dir"]) / "CLAUDE.md"
    assert claude_md.is_file()
    assert "客服助手" in claude_md.read_text(encoding="utf-8")


def test_initialize_business_agent_workspace_is_idempotent_and_preserves_edits(tmp_path: Path) -> None:
    from app.runtime.business_agent_workspace import initialize_business_agent_workspace

    workspace = tmp_path / "business-agents" / "soc-ops"
    initialize_business_agent_workspace(workspace, agent_id="soc-ops", name="客服助手")
    edited = "# 客服助手\n\n用户自定义行为配置\n"
    (workspace / "CLAUDE.md").write_text(edited, encoding="utf-8")

    edited_settings = '{"permissions": {"allow": ["Read(./**)"]}}\n'
    (workspace / ".claude" / "settings.json").write_text(edited_settings, encoding="utf-8")

    # 重复初始化不得覆盖用户编辑，且目录结构保持。
    initialize_business_agent_workspace(workspace, agent_id="soc-ops", name="客服助手")
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == edited
    assert (workspace / ".claude" / "settings.json").read_text(encoding="utf-8") == edited_settings
    assert (workspace / ".claude").is_dir()


def test_chat_routes_to_registered_business_agent(monkeypatch, tmp_path: Path) -> None:
    """AGV-024 基座：/api/chat 带 agent_id 路由到业务 Agent profile；缺省走 main；未知 404。"""
    from app.runtime.schemas import ChatResponse

    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}

    async def fake_run(req, *, profile=None, **kwargs):
        captured["profile"] = profile
        return ChatResponse(run_id="r", session_id="s", answer="ok")

    monkeypatch.setattr(module.runtime, "run", fake_run)
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})
        assert created.status_code == 201

        # 带 agent_id -> 路由到该业务 Agent 的 profile（被治理对象，cwd=其 workspace）。
        routed = client.post("/api/chat", json={"message": "hi", "agent_id": "soc-ops"})
        assert routed.status_code == 200
        assert captured["profile"] is not None
        assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops")
        assert captured["profile"].category == "business"

        # 缺省 agent_id -> 运行时默认 main agent（profile=None）。
        captured.clear()
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
        assert captured.get("profile") is None

        # 未知 agent_id -> 404，不静默回退到 main（避免错误归属）。
        assert client.post("/api/chat", json={"message": "hi", "agent_id": "biz-unknown"}).status_code == 404


def test_business_agent_has_active_lifecycle_status_by_default(monkeypatch, tmp_path: Path) -> None:
    """AGV-020 数据层：注册业务 Agent 默认生命周期状态 active，经 API 暴露；迁移回填既有行为 active。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})
        assert created.status_code == 201
        assert created.json()["status"] == "active"
        listed = {a["agent_id"]: a["status"] for a in client.get("/api/agent-registry").json()}
        assert listed["soc-ops"] == "active"
        # main-agent 种子也带 active 状态。
        assert listed["main-agent"] == "active"


def test_business_agent_lifecycle_transitions_and_archived_excluded_from_run(monkeypatch, tmp_path: Path) -> None:
    """AGV-020：合法生命周期转移被接受、非法转移被拒（可理解错误）、archived 不参与新运行。"""
    from app.runtime.schemas import ChatResponse

    module = _load_app(monkeypatch, tmp_path)

    async def fake_run(req, *, profile=None, **kwargs):
        return ChatResponse(run_id="r", session_id="s", answer="ok")

    monkeypatch.setattr(module.runtime, "run", fake_run)
    with TestClient(module.app) as client:
        client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})  # 默认 active
        # 合法转移：active -> deprecated -> archived。
        assert client.post("/api/agent-registry/soc-ops/lifecycle", json={"status": "deprecated"}).status_code == 200
        archived = client.post("/api/agent-registry/soc-ops/lifecycle", json={"status": "archived"})
        assert archived.status_code == 200 and archived.json()["status"] == "archived"
        # 非法转移：archived 为终态，archived -> active 被拒绝并返回可理解的状态机错误（409）。
        rejected = client.post("/api/agent-registry/soc-ops/lifecycle", json={"status": "active"})
        assert rejected.status_code == 409
        assert "transition" in rejected.json()["detail"].lower()
        # archived Agent 仍可查询（审计）但不参与新运行（400）。
        assert any(a["agent_id"] == "soc-ops" for a in client.get("/api/agent-registry").json())
        assert client.post("/api/chat", json={"message": "hi", "agent_id": "soc-ops"}).status_code == 400
        # main-agent 样板生命周期固定，不接受转移。
        assert client.post("/api/agent-registry/main-agent/lifecycle", json={"status": "archived"}).status_code == 400


def test_delete_business_agent_reports_impact_and_protects_main(monkeypatch, tmp_path: Path) -> None:
    """AGV-031：删除业务 Agent 给出治理影响面提示；main-agent 样板不可删，未知 404。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})
        # 该 Agent 的运行与反馈构成删除影响面。
        module.feedback_store.record_run({"run_id": "run-x", "agent_id": "soc-ops", "created_at": "2026-06-12T00:00:00Z"})
        client.post("/api/feedback-signals", json={"run_id": "run-x", "labels": ["tool_data_incomplete"]})

        deleted = client.delete("/api/agent-registry/soc-ops")
        assert deleted.status_code == 200
        body = deleted.json()
        assert body["deleted"]["agent_id"] == "soc-ops"
        # 删除前给出影响面提示：归属该 Agent 的运行与反馈被计入。
        assert body["impact"]["runs"] >= 1
        assert body["impact"]["feedback_signals"] >= 1
        # 删除后不再出现在注册表。
        assert "soc-ops" not in {a["agent_id"] for a in client.get("/api/agent-registry").json()}
        # main-agent 样板不可删（400），未知 agent_id 报 404。
        assert client.delete("/api/agent-registry/main-agent").status_code == 400
        assert client.delete("/api/agent-registry/biz-unknown").status_code == 404


def test_business_agent_workspace_scaffolds_safe_config_container(tmp_path: Path) -> None:
    """AGV-004：创建即得完整可编辑配置面（system prompt/skills/tools/MCP），且不泄露任何凭据。"""
    import json

    from app.runtime.business_agent_workspace import initialize_business_agent_workspace

    workspace = tmp_path / "business-agents" / "biz-1"
    initialize_business_agent_workspace(workspace, agent_id="biz-1", name="客服助手")

    # SDK 原生配置文件齐备：CLAUDE.md(system prompt) + settings.json(技能/工具) + .mcp.json(MCP)。
    claude_md = workspace / "CLAUDE.md"
    settings_path = workspace / ".claude" / "settings.json"
    mcp_path = workspace / ".mcp.json"
    assert claude_md.exists() and settings_path.exists() and mcp_path.exists()

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    # 起始权限保守：默认只读自身工作区、写/执行需确认、拒读 env/密钥。
    assert "Read(./.env)" in settings["permissions"]["deny"]
    assert settings["permissions"]["defaultMode"] == "default"
    # 起始 MCP 为空，不预置任何 server/header/凭据。
    assert mcp == {"mcpServers": {}}

    # 安全：配置容器任何文件都不得含 API key / MCP header / 本机私有路径（AGV-004 criterion 3）。
    # 注意区分"凭据值泄露"与 settings 里 Read(./secrets/**)、Read(./.env) 这类保护性 deny 规则——
    # 后者是防泄露规则而非泄露本身，故只校验凭据值模式与本机绝对路径。
    blob = "\n".join(p.read_text(encoding="utf-8") for p in (claude_md, settings_path, mcp_path)).lower()
    for forbidden in ("api_key", "apikey", "authorization", "sk-", "bearer ", "ghp_", str(tmp_path).lower()):
        assert forbidden not in blob, f"配置容器泄露了 {forbidden!r}"


def test_create_business_agent_generates_id_when_omitted(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "研发助手"})

    assert created.status_code == 201
    assert created.json()["agent_id"].startswith("biz-")
    assert created.json()["category"] == "business"
