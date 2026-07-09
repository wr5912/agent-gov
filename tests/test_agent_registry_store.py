"""AGV-004/022 基座：业务 Agent 身份注册表。

注册表只登记业务 Agent（被治理对象），治理 Agent（闭环执行者）不入表；
sync 幂等，作为运行/反馈/评估/版本治理的归属锚点。
"""

from __future__ import annotations

from pathlib import Path

from app.runtime.agent_paths import business_agent_layout, business_agents_root
from app.runtime.agent_profiles import build_profiles, discover_seeded_business_agents
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


def test_sync_updates_drifted_workspace_dir(tmp_path: Path) -> None:
    """⑤：已存在记录若 workspace_dir 漂移（升级后 main 从 /main-workspace 迁到 data/ 下），sync 同步更新。"""
    store, profiles = _store(tmp_path)
    store.create_business_agent(name="main-agent", agent_id="main-agent", workspace_dir="/main-workspace")
    assert store.get_agent("main-agent").workspace_dir == "/main-workspace"

    store.sync_business_agents(profiles)
    updated = store.get_agent("main-agent").workspace_dir
    assert updated != "/main-workspace"
    assert updated.endswith("/business-agents/main-agent/workspace")


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
    assert created.json()["workspace_dir"].endswith("/business-agents/soc-ops/workspace")
    assert {item["agent_id"] for item in listed.json()} == {"main-agent", "soc-ops"}
    assert duplicate.status_code == 409  # 重复身份被拒绝，不污染既有 Agent
    assert empty_name.status_code == 400  # 空名校验
    # 运行态前提：创建即初始化 workspace 与起始 CLAUDE.md。
    claude_md = Path(created.json()["workspace_dir"]) / "CLAUDE.md"
    assert claude_md.is_file()
    assert "客服助手" in claude_md.read_text(encoding="utf-8")


def test_create_business_agent_rejects_path_traversal_agent_id(monkeypatch, tmp_path: Path) -> None:
    """③ 越权输入：含路径穿越/分隔符/非法字符的 agent_id 不得用于 workspace 落地路径，→ 422。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        for hostile in ["../escape", "a/b", "..", ".", "with space", "x/../../etc"]:
            resp = client.post("/api/agent-registry", json={"name": "恶意归属", "agent_id": hostile})
            assert resp.status_code == 422, f"{hostile!r} 应 422，实际 {resp.status_code}"


def test_create_with_template_id_seeds_from_template_and_unknown_rejected(monkeypatch, tmp_path: Path) -> None:
    """F10：经 API 传 template_id 创建——合法模板播种 workspace；未知 template_id → 422；GET /templates 列 catalog。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        templates = client.get("/api/agent-registry/templates")
        ok = client.post("/api/agent-registry", json={"name": "模板助手", "agent_id": "tpl-ok", "template_id": "general"})
        unknown = client.post(
            "/api/agent-registry", json={"name": "未知模板", "agent_id": "tpl-bad", "template_id": "does-not-exist"}
        )

    assert templates.status_code == 200
    assert "general" in templates.json()["templates"]
    assert ok.status_code == 201
    # 按模板播种：workspace 起始 CLAUDE.md 存在且渲染了 agent 名称。
    claude_md = Path(ok.json()["workspace_dir"]) / "CLAUDE.md"
    assert claude_md.is_file() and "模板助手" in claude_md.read_text(encoding="utf-8")
    assert unknown.status_code == 422  # 未知 template_id 不静默回退


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
    """AGV-024 基座：/api/chat 带 agent_id 路由到业务 Agent profile；缺省 agent_id 422；未知 404。"""
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
        assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")
        assert captured["profile"].category == "business"

        # 缺省 agent_id -> 422（两个原生入口 agent_id 必填，不静默跑 main）。
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 422

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


def test_feedback_asset_provenance_traces_agent_and_relationship(monkeypatch, tmp_path: Path) -> None:
    """AGV-022：从某次反馈可追溯资产关系——影响了哪个 Agent、改了哪些资产、进入哪个版本。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(monkeypatch, tmp_path)
    fs = module.feedback_store
    fs.record_run({"run_id": "run-x", "agent_id": "soc-ops", "created_at": "2026-06-12T00:00:00Z"})
    signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-x", labels=["tool_data_incomplete"]))
    case = fs.create_case(source_ids=[signal["signal_id"]], title="数据标准化反馈")
    case_id = case["feedback_case_id"]
    improvement = module.improvement_store.create_improvement(
        agent_id="soc-ops",
        title="数据标准化映射治理",
        source_feedback_refs=[case_id],
    )
    module.improvement_store.add_link(improvement.improvement_id, kind="change_set", ref_id="agc-test")

    with TestClient(module.app) as client:
        prov = client.get(f"/api/asset-registry/feedback/{case_id}")
        assert prov.status_code == 200
        body = prov.json()
        assert body["feedback_case_id"] == case_id
        # 影响了哪个 Agent：从反馈归属可追溯。
        assert "soc-ops" in body["agent_ids"]
        assert body["improvements"][0]["improvement_id"] == improvement.improvement_id
        assert body["improvements"][0]["change_set_ids"] == ["agc-test"]
        # 未知 case -> 404。
        assert client.get("/api/asset-registry/feedback/nope").status_code == 404


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
    """AGV-031：统一入口下 agent_id 在运行/反馈/评估/版本一致，删除前给出跨维度影响面提示；main-agent 样板不可删，未知 404。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(monkeypatch, tmp_path)
    fs = module.feedback_store
    gov = module.agent_governance
    with TestClient(module.app) as client:
        # 统一入口创建一个治理对象，后续运行/反馈/评估/版本都用同一 agent_id 串联。
        client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})
        fs.record_run({"run_id": "run-x", "agent_id": "soc-ops", "created_at": "2026-06-12T00:00:00Z"})
        signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-x", labels=["tool_data_incomplete"]))
        # 版本维度：该 Agent 独立 change set → release（落自己的版本 store）。
        change_set = gov.create_change_set(title="soc-ops 候选", operator="t", agent_id="soc-ops")
        worktree = Path(str(change_set["worktree_path"]))
        worktree.joinpath("CLAUDE.md").write_text("# soc-ops\n", encoding="utf-8")
        commit = gov._store_for("soc-ops").commit_worktree(worktree, message="c")
        gov.mark_candidate_committed(str(change_set["change_set_id"]), candidate_commit_sha=commit, execution_job_id=None, operator="t")
        release = gov.publish_change_set(str(change_set["change_set_id"]), operator="t")
        # 评估维度：eval run 锚定该 Agent 的 change set → 继承归属。
        eval_run = fs.create_eval_run(eval_case_ids=[], agent_version_id=release["commit_sha"], change_set_id=str(change_set["change_set_id"]))

        # Agent ID 在运行/反馈/评估/版本中保持一致（统一治理对象，无需跨入口手工拼接）。
        assert signal["agent_id"] == "soc-ops"
        assert eval_run["agent_id"] == "soc-ops"
        assert change_set["agent_id"] == "soc-ops" and release["agent_id"] == "soc-ops"

        deleted = client.delete("/api/agent-registry/soc-ops")
        assert deleted.status_code == 200
        body = deleted.json()
        assert body["deleted"]["agent_id"] == "soc-ops"
        # 删除前给出跨维度影响面提示：运行/反馈/评估/版本均计入，避免无声删除治理对象。
        impact = body["impact"]
        assert impact["runs"] >= 1 and impact["feedback_signals"] >= 1
        assert impact["eval_runs"] >= 1
        assert impact["change_sets"] >= 1 and impact["releases"] >= 1
        # 删除后不再出现在注册表。
        assert "soc-ops" not in {a["agent_id"] for a in client.get("/api/agent-registry").json()}
        # main-agent 样板不可删（400），未知 agent_id 报 404。
        assert client.delete("/api/agent-registry/main-agent").status_code == 400
        assert client.delete("/api/agent-registry/biz-unknown").status_code == 404


def test_main_agent_paradigm_generalizes_to_new_business_agent(monkeypatch, tmp_path: Path) -> None:
    """AGV-044：main agent 闭环范式抽象为通用治理模型——新业务 Agent 经同一注册表/版本 store/agent_id 抽象复用全部能力，无需复制 main 硬编码路径，且反馈/版本/评估按 Agent 隔离。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(monkeypatch, tmp_path)
    fs = module.feedback_store
    gov = module.agent_governance
    with TestClient(module.app) as client:
        # main agent 本身是注册表首条记录（样板=首个被注册业务 Agent，而非硬编码特例）。
        assert "main-agent" in {a["agent_id"] for a in client.get("/api/agent-registry").json()}
        # 接入一个新业务 Agent，与 main 走同一创建入口（明确接入步骤）。
        assert client.post("/api/agent-registry", json={"name": "电商助手", "agent_id": "shop-bot"}).status_code == 201
        assert "shop-bot" in {a["agent_id"] for a in client.get("/api/agent-registry").json()}

        # 复用运行/反馈/评估/版本能力，全部经 agent_id 归属（非复制 main 路径）。
        fs.record_run({"run_id": "run-s", "agent_id": "shop-bot", "created_at": "2026-06-12T00:00:00Z"})
        signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-s", labels=["tool_data_incomplete"]))
        change_set = gov.create_change_set(title="shop-bot 候选", operator="t", agent_id="shop-bot")
        worktree = Path(str(change_set["worktree_path"]))
        worktree.joinpath("CLAUDE.md").write_text("# shop-bot\n", encoding="utf-8")
        commit = gov._store_for("shop-bot").commit_worktree(worktree, message="c")
        gov.mark_candidate_committed(str(change_set["change_set_id"]), candidate_commit_sha=commit, execution_job_id=None, operator="t")
        release = gov.publish_change_set(str(change_set["change_set_id"]), operator="t")
        eval_run = fs.create_eval_run(eval_case_ids=[], agent_version_id=release["commit_sha"], change_set_id=str(change_set["change_set_id"]))

        # 同一抽象、不同实例：main 与新 Agent 版本 store 经同一 _store_for 工厂取，物理隔离（非硬编码 main 路径）。
        assert gov._store_for("shop-bot") is not gov._store_for("main-agent")
        main_head = gov._store_for("main-agent").current_commit_sha()
        # 反馈/版本/评估按 Agent 维度隔离，main 版本链不被新 Agent 发布污染。
        assert signal["agent_id"] == "shop-bot" and eval_run["agent_id"] == "shop-bot"
        shop_cs = {c["change_set_id"] for c in gov.list_change_sets(agent_id="shop-bot")}
        assert shop_cs == {change_set["change_set_id"]}
        assert shop_cs.isdisjoint({c["change_set_id"] for c in gov.list_change_sets(agent_id="main-agent")})
        assert gov._store_for("shop-bot").current_commit_sha() == release["commit_sha"]
        assert gov._store_for("main-agent").current_commit_sha() == main_head


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
    # 起始权限保守：默认只读自身工作区，Bash 直行但仍由 sandbox/hook/deny 拦截；写入工作区需确认。
    assert "Read(./.env)" in settings["permissions"]["deny"]
    assert "Bash(*)" in settings["permissions"]["allow"]
    assert "Bash(*)" not in settings["permissions"]["ask"]
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


def _settings_with_data_dir(monkeypatch, tmp_path: Path) -> AppSettings:
    """构造 data_dir 指向 tmp 的设置，用于隔离发现逻辑的磁盘扫描。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return AppSettings()


def test_discover_seeded_business_agents_finds_all_disk_agents(monkeypatch, tmp_path: Path) -> None:
    """seed 把多个业务 Agent 落到 data/business-agents/* 后，发现逻辑识别全部（不止 main-agent）。"""
    settings = _settings_with_data_dir(monkeypatch, tmp_path)
    for agent_id in ("main-agent", "AAA", "BBB"):
        business_agent_layout(settings.data_dir, agent_id).workspace.mkdir(parents=True, exist_ok=True)

    discovered = {profile.name: profile for profile in discover_seeded_business_agents(settings)}

    assert set(discovered) == {"main-agent", "AAA", "BBB"}
    aaa = discovered["AAA"]
    # 与 main-agent 走同一抽象：category=business、name=agent_id、workspace_dir 由 layout 单一真相派生。
    assert aaa.category == "business"
    assert aaa.name == "AAA"
    assert str(aaa.workspace_dir).endswith("/business-agents/AAA/workspace")


def test_discover_returns_empty_when_business_agents_root_absent(monkeypatch, tmp_path: Path) -> None:
    """运行卷尚未 bootstrap（business-agents 目录不存在）时发现逻辑安全返回空，不抛错。"""
    settings = _settings_with_data_dir(monkeypatch, tmp_path)
    assert discover_seeded_business_agents(settings) == []


def test_discover_skips_invalid_and_non_agent_entries(monkeypatch, tmp_path: Path) -> None:
    """③ 外部输入（磁盘目录名）异常/越权：非法 agent_id、缺 workspace、非目录条目一律跳过，不污染注册表。"""
    settings = _settings_with_data_dir(monkeypatch, tmp_path)
    root = business_agents_root(settings.data_dir)
    root.mkdir(parents=True, exist_ok=True)
    # 合法业务 Agent：有 workspace/。
    business_agent_layout(settings.data_dir, "AAA").workspace.mkdir(parents=True, exist_ok=True)
    # 非法 agent_id（含空格）即使有 workspace 也跳过（防注入/穿越）。
    (root / "bad name" / "workspace").mkdir(parents=True, exist_ok=True)
    # 缺 workspace/ 的目录跳过（非有效业务 Agent，如残留/备份目录）。
    (root / "no-workspace").mkdir(parents=True, exist_ok=True)
    # 非目录条目跳过。
    (root / "stray.txt").write_text("x", encoding="utf-8")

    discovered_ids = {profile.name for profile in discover_seeded_business_agents(settings)}

    assert discovered_ids == {"AAA"}
    assert "bad name" not in discovered_ids
    assert "no-workspace" not in discovered_ids


def test_lifespan_auto_registers_seeded_business_agents(monkeypatch, tmp_path: Path) -> None:
    """端到端：seed 到磁盘的 AAA/BBB 在应用启动时被自动登记，可经 API 列出并被 chat 路由到（认得）。"""
    from app.runtime.schemas import ChatResponse

    module = _load_app(monkeypatch, tmp_path)
    data_dir = module.settings.data_dir
    # 模拟 bootstrap：把两个业务 Agent 的 workspace 落盘（不经 POST 创建）。
    for agent_id in ("AAA", "BBB"):
        ws = business_agent_layout(data_dir, agent_id).workspace
        ws.mkdir(parents=True, exist_ok=True)
        ws.joinpath("CLAUDE.md").write_text(f"# {agent_id}\n", encoding="utf-8")

    captured: dict = {}

    async def fake_run(req, *, profile=None, **kwargs):
        captured["profile"] = profile
        return ChatResponse(run_id="r", session_id="s", answer="ok")

    monkeypatch.setattr(module.runtime, "run", fake_run)
    with TestClient(module.app) as client:
        listed = client.get("/api/agent-registry").json()
        ids = {a["agent_id"] for a in listed}
        # 磁盘 seed 的 AAA/BBB 与预制 main-agent 一同进入注册表。
        assert {"main-agent", "AAA", "BBB"} <= ids
        seeded = next(a for a in listed if a["agent_id"] == "AAA")
        assert seeded["category"] == "business"
        assert seeded["status"] == "active"
        assert seeded["workspace_dir"].endswith("/business-agents/AAA/workspace")

        # 端到端"认得"：chat 路由到自动登记的 AAA（每请求动态构造其业务 profile）。
        routed = client.post("/api/chat", json={"message": "hi", "agent_id": "AAA"})
        assert routed.status_code == 200
        assert str(captured["profile"].workspace_dir).endswith("/business-agents/AAA/workspace")
        assert captured["profile"].category == "business"


def test_lifespan_discovery_keeps_main_agent_single_row_across_restarts(monkeypatch, tmp_path: Path) -> None:
    """并发/重复：main-agent 同时来自 build_profiles 与磁盘发现两源，重复启动 sync 不得重复登记。"""
    module = _load_app(monkeypatch, tmp_path)  # _load_app 预建 data/business-agents/main-agent/workspace
    with TestClient(module.app):
        pass
    first = [a.agent_id for a in module.agent_registry_store.list_agents()]
    assert first.count("main-agent") == 1
    # 再次进入 lifespan（模拟重启）：双源 main-agent 仍单行。
    with TestClient(module.app):
        pass
    second = [a.agent_id for a in module.agent_registry_store.list_agents()]
    assert second.count("main-agent") == 1
