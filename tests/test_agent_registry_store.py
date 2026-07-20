"""AGV-004/022 基座：业务 Agent 身份注册表。

注册表只登记业务 Agent（被治理对象），治理 Agent（闭环执行者）不入表；
sync 幂等，作为运行/反馈/评估/版本治理的归属锚点。
"""

from __future__ import annotations

from pathlib import Path

from app.runtime.agent_paths import business_agent_layout, business_agents_root
from app.runtime.agent_profiles import build_business_agent_profile, build_profiles, discover_business_agents
from app.runtime.protected_business_agents import (
    DEFAULT_BUSINESS_AGENT_ID,
    SECURITY_OPERATIONS_EXPERT_AGENT_ID,
)
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from fastapi.testclient import TestClient

from app_test_utils import load_test_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID
from test_agent_workspace_packages import _import_new_agent


def _load_app(monkeypatch, tmp_path, **kwargs):
    return load_test_app(
        monkeypatch,
        tmp_path,
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
        **kwargs,
    )


def _store(tmp_path: Path) -> tuple[AgentRegistryStore, dict]:
    """注册表 store + 一份「governor（治理执行者）+ 一个业务 Agent」的 profile 集合。

    业务 profile 在此显式构造；`build_profiles` 只提供 governor，业务 Agent 在生产中由磁盘发现。
    """

    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    settings = AppSettings()
    profiles = build_profiles(settings)
    profiles[DEFAULT_BUSINESS_AGENT_ID] = build_business_agent_profile(
        settings,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        workspace_dir=settings.default_workspace_dir,
    )
    return AgentRegistryStore(factory), profiles


def _record_passed_test_run(module, *, agent_id: str, commit_sha: str, change_set_id: str) -> dict:
    suite = module.agent_testing_service.inspect_suite(agent_id, commit_sha=commit_sha)
    assert suite.runnable
    assert suite.suite_digest
    run = module.agent_testing_store.create_run(
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=change_set_id,
        source="release_check",
        command=["python", "-m", "pytest", "-q", "-p", "agentgov_testkit.pytest_plugin", "tests"],
        suite=suite.model_dump(mode="json"),
        suite_digest=suite.suite_digest,
    )
    claimed = module.agent_testing_store.claim_run(str(run["test_run_id"]))
    assert claimed is not None
    return module.agent_testing_store.finish_run(
        str(run["test_run_id"]),
        status="passed",
        report={"passed": 1, "failed": 0},
        items=[{"nodeid": "tests/test_agent.py::test_agent", "outcome": "passed"}],
        stdout="1 passed",
        stderr="",
    )


def _write_runnable_agent_test(worktree: Path) -> None:
    tests_dir = worktree / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("README.md").write_text("# Agent tests\n", encoding="utf-8")
    tests_dir.joinpath("test_agent.py").write_text(
        "def test_agent(agent):\n"
        "    result = agent.run('仅依据以下已给定事实回答，不调用任何工具或读取文件。回答必须包含测试通过。')\n"
        "    assert not result.errors\n"
        "    normalized_text = ''.join(result.text.split())\n"
        "    assert '测试通过' in normalized_text\n"
        "    assert result.raw['agent_activity']['tool_calls'] == []\n",
        encoding="utf-8",
    )


def test_sync_registers_only_business_agents(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    store.sync_business_agents(profiles)

    agents = store.list_agents()
    assert [agent.agent_id for agent in agents] == [DEFAULT_BUSINESS_AGENT_ID]
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

    record = store.get_agent(DEFAULT_BUSINESS_AGENT_ID)
    assert record is not None
    assert record.name == DEFAULT_BUSINESS_AGENT_ID
    assert record.workspace_dir  # 非空 workspace，作为归属锚点
    assert record.created_at


def test_hitl_observation_is_derived_from_current_project_settings(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    workspace = tmp_path / "workspace"
    settings_path = workspace / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        '{"permissions":{"ask":["mcp__approval__execute"]}}\n',
        encoding="utf-8",
    )
    store.create_business_agent(
        name="SOC",
        agent_id="soc-ops",
        workspace_dir=str(workspace),
    )

    assert store.get_agent("soc-ops").requires_web_hitl is True

    settings_path.write_text('{"permissions":{"ask":[]}}\n', encoding="utf-8")
    assert store.get_agent("soc-ops").requires_web_hitl is False

    store.sync_business_agents(profiles)
    assert store.get_agent("soc-ops").requires_web_hitl is False


def test_sync_updates_drifted_workspace_dir(tmp_path: Path) -> None:
    """已存在记录的 workspace_dir 漂移时，磁盘发现结果负责同步当前路径。"""
    store, profiles = _store(tmp_path)
    store.create_business_agent(
        name=DEFAULT_BUSINESS_AGENT_ID,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        workspace_dir="/stale-workspace",
    )
    assert store.get_agent(DEFAULT_BUSINESS_AGENT_ID).workspace_dir == "/stale-workspace"

    store.sync_business_agents(profiles)
    updated = store.get_agent(DEFAULT_BUSINESS_AGENT_ID).workspace_dir
    assert updated != "/stale-workspace"
    assert updated.endswith(f"/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/workspace")


def test_lifespan_syncs_discovered_business_agent_registry(monkeypatch, tmp_path: Path) -> None:
    """应用启动（lifespan）幂等登记业务 Agent，使注册表在运行态被真实消费。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app):
        pass

    agents = module.agent_registry_store.list_agents()
    assert {agent.agent_id for agent in agents} == {ORDINARY_TEST_AGENT_ID, DEFAULT_BUSINESS_AGENT_ID}
    assert all(agent.category == "business" for agent in agents)


def test_lifespan_leaves_legacy_sdk_session_for_demand_driven_migration(monkeypatch, tmp_path: Path) -> None:
    """API readiness 不得被历史 transcript 全量迁移阻塞；恢复/历史读取路径负责按需迁移。"""
    module = _load_app(monkeypatch, tmp_path)
    session = module.session_store.get_or_create_owned("legacy-session", agent_id=DEFAULT_BUSINESS_AGENT_ID)
    session.sdk_session_id = "00000000-0000-4000-8000-000000000001"
    module.session_store.save(session)

    with TestClient(module.app) as client:
        assert client.get("/health/live").status_code == 200

    persisted = module.session_store.get(session.session_id)
    assert persisted is not None
    assert persisted.sdk_store_ready_at is None
    assert persisted.sdk_store_migration_error is None


def test_list_agents_endpoint_returns_registered_business_agents(monkeypatch, tmp_path: Path) -> None:
    """AGV-004/007：注册的业务 Agent 定义可经 API 查询，作为外部接入与归属对象的可见入口。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        response = client.get("/api/agent-registry")

    assert response.status_code == 200
    body = response.json()
    assert {item["agent_id"] for item in body} == {ORDINARY_TEST_AGENT_ID, DEFAULT_BUSINESS_AGENT_ID}
    assert all(item["category"] == "business" for item in body)
    assert all(item["workspace_dir"] for item in body)
    default = next(item for item in body if item["agent_id"] == DEFAULT_BUSINESS_AGENT_ID)
    assert default["builtin"] is True and default["default"] is True and default["protected"] is True


def test_direct_create_and_template_catalog_endpoints_are_removed(monkeypatch, tmp_path: Path) -> None:
    """新 Agent 只能通过 Workspace 包导入，旧创建入口和模板 catalog 不保留兼容层。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        direct_create = client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})
        template_catalog = client.get("/api/agent-registry/templates")

    assert direct_create.status_code == 405
    assert template_catalog.status_code == 405


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
        created = _import_new_agent(client, agent_id="soc-ops", name="客服助手")
        assert created.status_code == 200

        # 带 agent_id -> 路由到该业务 Agent 的 profile（被治理对象，cwd=其 workspace）。
        routed = client.post("/api/chat", json={"message": "hi", "agent_id": "soc-ops"})
        assert routed.status_code == 200
        assert captured["profile"] is not None
        assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")
        assert captured["profile"].category == "business"

        # 缺省 agent_id -> 422（两个原生入口 agent_id 必填，不静默跑平台默认）。
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 422

        # 未知 agent_id -> 404，不静默回退到平台默认（避免错误归属）。
        assert client.post("/api/chat", json={"message": "hi", "agent_id": "biz-unknown"}).status_code == 404


def test_business_agent_has_active_lifecycle_status_by_default(monkeypatch, tmp_path: Path) -> None:
    """AGV-020 数据层：注册业务 Agent 默认生命周期状态 active，经 API 暴露；迁移回填既有行为 active。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="soc-ops", name="客服助手")
        assert created.status_code == 200
        assert created.json()["agent"]["status"] == "active"
        listed = {a["agent_id"]: a["status"] for a in client.get("/api/agent-registry").json()}
        assert listed["soc-ops"] == "active"
        assert listed[DEFAULT_BUSINESS_AGENT_ID] == "active"
        assert listed[ORDINARY_TEST_AGENT_ID] == "active"


def test_feedback_asset_provenance_traces_agent_and_relationship(monkeypatch, tmp_path: Path) -> None:
    """AGV-022：从某次反馈可追溯资产关系——影响了哪个 Agent、改了哪些资产、进入哪个版本。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(monkeypatch, tmp_path)
    fs = module.feedback_store
    module.agent_registry_store.create_business_agent(
        name="SOC Ops",
        agent_id="soc-ops",
        workspace_dir=str(module.settings.data_dir / "business-agents" / "soc-ops" / "workspace"),
    )
    fs.record_run({"run_id": "run-x", "agent_id": "soc-ops", "created_at": "2026-06-12T00:00:00Z"})
    signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-x", labels=["tool_data_incomplete"]))
    case = fs.create_case(source_refs=[("signal", signal["signal_id"])], title="数据标准化反馈")
    case_id = case["feedback_case_id"]
    improvement = module.improvement_store.create_improvement(
        agent_id="soc-ops",
        title="数据标准化映射治理",
    )
    module.improvement_content_store.attach_feedback_case(
        improvement.improvement_id,
        agent_id="soc-ops",
        feedback_case_id=case_id,
        summary=case["title"],
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
        _import_new_agent(client, agent_id="soc-ops", name="客服助手")  # 默认 active
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
        # 普通业务 Agent 与导入 Agent 使用同一生命周期状态机。
        ordinary_lifecycle = client.post(
            f"/api/agent-registry/{ORDINARY_TEST_AGENT_ID}/lifecycle",
            json={"status": "archived"},
        )
        assert ordinary_lifecycle.status_code == 200


def test_delete_business_agent_reports_impact_and_protects_builtin_agent(monkeypatch, tmp_path: Path) -> None:
    """AGV-031：统一入口下 agent_id 在运行、反馈、测试和版本中一致。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(monkeypatch, tmp_path)
    fs = module.feedback_store
    gov = module.agent_governance
    with TestClient(module.app) as client:
        # 统一入口创建一个治理对象，后续运行、反馈、测试和版本都用同一 agent_id 串联。
        _import_new_agent(client, agent_id="soc-ops", name="客服助手")
        fs.record_run({"run_id": "run-x", "agent_id": "soc-ops", "created_at": "2026-06-12T00:00:00Z"})
        signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-x", labels=["tool_data_incomplete"]))
        # 版本维度：该 Agent 独立 change set → release（落自己的版本 store）。
        change_set = gov.create_change_set(title="soc-ops 候选", operator="t", agent_id="soc-ops")
        worktree = Path(str(change_set["worktree_path"]))
        worktree.joinpath("CLAUDE.md").write_text("# soc-ops\n", encoding="utf-8")
        _write_runnable_agent_test(worktree)
        commit = gov._store_for("soc-ops").commit_worktree(worktree, message="c")
        change_set = gov.mark_candidate_committed(
            str(change_set["change_set_id"]),
            candidate_commit_sha=commit,
            execution_job_id=None,
            operator="t",
        )
        test_run = _record_passed_test_run(
            module,
            agent_id="soc-ops",
            commit_sha=commit,
            change_set_id=str(change_set["change_set_id"]),
        )
        release = gov.publish_change_set(str(change_set["change_set_id"]), operator="t")

        # Agent ID 在运行、反馈、测试和版本中保持一致。
        assert signal["agent_id"] == "soc-ops"
        assert test_run["agent_id"] == "soc-ops"
        assert change_set["agent_id"] == "soc-ops" and release["agent_id"] == "soc-ops"

        deleted = client.delete("/api/agent-registry/soc-ops")
        assert deleted.status_code == 200
        body = deleted.json()
        assert body["deleted"]["agent_id"] == "soc-ops"
        # 删除前给出跨维度影响面提示：运行、反馈、测试和版本均计入。
        impact = body["impact"]
        assert impact["runs"] >= 1 and impact["feedback_signals"] >= 1
        assert impact["test_runs"] >= 1
        assert impact["change_sets"] >= 1 and impact["releases"] >= 1
        # 删除后不再出现在注册表。
        assert "soc-ops" not in {a["agent_id"] for a in client.get("/api/agent-registry").json()}
        # 受保护的内置 Agent 不可删（400）；未知 agent_id 报 404。
        assert client.delete(f"/api/agent-registry/{SECURITY_OPERATIONS_EXPERT_AGENT_ID}").status_code == 400
        assert client.delete("/api/agent-registry/biz-unknown").status_code == 404


def test_workspace_imported_business_agents_share_governance_without_builtin_special_cases(monkeypatch, tmp_path: Path) -> None:
    """AGV-044：Workspace 包接入的 Agent 与内置 Agent 复用同一治理抽象并保持隔离。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(monkeypatch, tmp_path)
    fs = module.feedback_store
    gov = module.agent_governance
    with TestClient(module.app) as client:
        assert DEFAULT_BUSINESS_AGENT_ID in {a["agent_id"] for a in client.get("/api/agent-registry").json()}
        # 通过唯一创建入口接入一个新业务 Agent。
        assert _import_new_agent(client, agent_id="shop-bot", name="电商助手").status_code == 200
        assert "shop-bot" in {a["agent_id"] for a in client.get("/api/agent-registry").json()}

        # 复用运行、反馈、测试和版本能力，全部经 agent_id 归属。
        fs.record_run({"run_id": "run-s", "agent_id": "shop-bot", "created_at": "2026-06-12T00:00:00Z"})
        signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-s", labels=["tool_data_incomplete"]))
        change_set = gov.create_change_set(title="shop-bot 候选", operator="t", agent_id="shop-bot")
        worktree = Path(str(change_set["worktree_path"]))
        worktree.joinpath("CLAUDE.md").write_text("# shop-bot\n", encoding="utf-8")
        _write_runnable_agent_test(worktree)
        commit = gov._store_for("shop-bot").commit_worktree(worktree, message="c")
        change_set = gov.mark_candidate_committed(
            str(change_set["change_set_id"]),
            candidate_commit_sha=commit,
            execution_job_id=None,
            operator="t",
        )
        test_run = _record_passed_test_run(
            module,
            agent_id="shop-bot",
            commit_sha=commit,
            change_set_id=str(change_set["change_set_id"]),
        )
        release = gov.publish_change_set(str(change_set["change_set_id"]), operator="t")

        # 同一抽象、不同实例：内置 Agent 与新 Agent 的版本 store 物理隔离。
        assert gov._store_for("shop-bot") is not gov._store_for(DEFAULT_BUSINESS_AGENT_ID)
        builtin_head = gov._store_for(DEFAULT_BUSINESS_AGENT_ID).current_commit_sha()
        # 反馈、版本和测试按 Agent 维度隔离，内置 Agent 版本链不被新 Agent 发布污染。
        assert signal["agent_id"] == "shop-bot" and test_run["agent_id"] == "shop-bot"
        shop_cs = {c["change_set_id"] for c in gov.list_change_sets(agent_id="shop-bot")}
        assert shop_cs == {change_set["change_set_id"]}
        assert shop_cs.isdisjoint({c["change_set_id"] for c in gov.list_change_sets(agent_id=DEFAULT_BUSINESS_AGENT_ID)})
        assert gov._store_for("shop-bot").current_commit_sha() == release["commit_sha"]
        assert gov._store_for(DEFAULT_BUSINESS_AGENT_ID).current_commit_sha() == builtin_head


def _settings_with_data_dir(monkeypatch, tmp_path: Path) -> AppSettings:
    """构造 data_dir 指向 tmp 的设置，用于隔离发现逻辑的磁盘扫描。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return AppSettings()


def test_discover_business_agents_finds_all_live_workspaces(monkeypatch, tmp_path: Path) -> None:
    """运行态存在多个业务 Agent Workspace 时，发现逻辑识别全部。"""
    settings = _settings_with_data_dir(monkeypatch, tmp_path)
    for agent_id in (ORDINARY_TEST_AGENT_ID, "AAA", "BBB"):
        business_agent_layout(settings.data_dir, agent_id).workspace.mkdir(parents=True, exist_ok=True)

    discovered = {profile.name: profile for profile in discover_business_agents(settings)}

    assert set(discovered) == {ORDINARY_TEST_AGENT_ID, "AAA", "BBB"}
    aaa = discovered["AAA"]
    # 全部走同一抽象：category=business、name=agent_id、workspace_dir 由 layout 单一真相派生。
    assert aaa.category == "business"
    assert aaa.name == "AAA"
    assert str(aaa.workspace_dir).endswith("/business-agents/AAA/workspace")


def test_discover_returns_empty_when_business_agents_root_absent(monkeypatch, tmp_path: Path) -> None:
    """运行卷尚未 bootstrap（business-agents 目录不存在）时发现逻辑安全返回空，不抛错。"""
    settings = _settings_with_data_dir(monkeypatch, tmp_path)
    assert discover_business_agents(settings) == []


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

    discovered_ids = {profile.name for profile in discover_business_agents(settings)}

    assert discovered_ids == {"AAA"}
    assert "bad name" not in discovered_ids
    assert "no-workspace" not in discovered_ids


def test_lifespan_auto_registers_live_business_agent_workspaces(monkeypatch, tmp_path: Path) -> None:
    """端到端：磁盘上的 AAA/BBB Workspace 在应用启动时被登记并可被 chat 路由。"""
    from app.runtime.schemas import ChatResponse

    module = _load_app(monkeypatch, tmp_path)
    data_dir = module.settings.data_dir
    # 模拟外部导入已落盘：把两个业务 Agent 的 Workspace 放入运行卷。
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
        # 磁盘上的 AAA/BBB 与夹具已有业务 Agent 一同进入注册表。
        assert {DEFAULT_BUSINESS_AGENT_ID, ORDINARY_TEST_AGENT_ID, "AAA", "BBB"} <= ids
        discovered = next(a for a in listed if a["agent_id"] == "AAA")
        assert discovered["category"] == "business"
        assert discovered["status"] == "active"
        assert discovered["workspace_dir"].endswith("/business-agents/AAA/workspace")

        # 端到端"认得"：chat 路由到自动登记的 AAA（每请求动态构造其业务 profile）。
        routed = client.post("/api/chat", json={"message": "hi", "agent_id": "AAA"})
        assert routed.status_code == 200
        assert str(captured["profile"].workspace_dir).endswith("/business-agents/AAA/workspace")
        assert captured["profile"].category == "business"


def test_lifespan_discovery_keeps_each_business_agent_single_row_across_restarts(monkeypatch, tmp_path: Path) -> None:
    """重复启动 sync 不得为已发现业务 Agent 产生重复记录。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app):
        pass
    first = [a.agent_id for a in module.agent_registry_store.list_agents()]
    assert first.count(ORDINARY_TEST_AGENT_ID) == 1
    assert first.count(DEFAULT_BUSINESS_AGENT_ID) == 1
    # 再次进入 lifespan（模拟重启）后仍各一行。
    with TestClient(module.app):
        pass
    second = [a.agent_id for a in module.agent_registry_store.list_agents()]
    assert second.count(ORDINARY_TEST_AGENT_ID) == 1
    assert second.count(DEFAULT_BUSINESS_AGENT_ID) == 1
