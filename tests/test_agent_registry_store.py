"""AGV-004/022 基座：业务 Agent 身份注册表。

注册表只登记业务 Agent（被治理对象），治理 Agent（闭环执行者）不入表；
sync 幂等，作为运行/反馈/评估/版本治理的归属锚点。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agent_testing.runner import FIXED_PYTEST_COMMAND
from app.runtime.agent_paths import business_agent_layout, business_agents_root
from app.runtime.agent_profiles import build_business_agent_profile, build_profiles, discover_business_agents
from app.runtime.protected_business_agents import (
    DEFAULT_BUSINESS_AGENT_ID,
    SECURITY_OPERATIONS_EXPERT_AGENT_ID,
)
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.services.agent_candidate_approval import inspect_candidate_review
from app.services.agent_governance_errors import AgentGovernanceError
from app.services.agent_governance_projections import candidate_diff_digest
from fastapi.testclient import TestClient

from agent_release_test_utils import install_release_activation_boundary
from app_test_utils import load_test_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID, create_test_business_agent_workspace
from test_agent_workspace_packages import (
    _agent_manifest,
    _import_new_agent,
    _seed_active_agent,
    _workspace_package,
)


def _load_app(process_environment, tmp_path, **kwargs):
    process_environment.set("RUNTIME_CANDIDATES_DIR", str(tmp_path / "candidate-workspaces"))
    module = load_test_app(
        process_environment,
        tmp_path,
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
        **kwargs,
    )
    install_release_activation_boundary(module.agent_governance)
    return module


def _store(tmp_path: Path) -> tuple[AgentRegistryStore, dict]:
    """注册表 store + 一份「governor（治理执行者）+ 一个业务 Agent」的 profile 集合。

    业务 profile 在此显式构造；`build_profiles` 只提供 governor，业务 Agent 在生产中由磁盘发现。
    """

    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    settings = AppSettings(_env_file=None, AGENTGOV_RUNTIME_SHARED_SECRET="test-runtime-shared-secret")
    profiles = build_profiles(settings)
    profiles[DEFAULT_BUSINESS_AGENT_ID] = build_business_agent_profile(
        settings,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        workspace_dir=settings.default_workspace_dir,
    )
    return AgentRegistryStore(factory), profiles


def _record_unrun_test_run(module, *, agent_id: str, commit_sha: str, change_set_id: str) -> dict:
    """只登记待执行测试以验证删除影响面；绝不冒充发布所需的真实通过证据。"""
    suite = module.agent_testing_service.inspect_suite(agent_id, commit_sha=commit_sha)
    assert suite.runnable
    assert suite.suite_digest
    return module.agent_testing_store.create_run(
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=change_set_id,
        source="release_check",
        command=FIXED_PYTEST_COMMAND,
        suite=suite.model_dump(mode="json"),
        suite_digest=suite.suite_digest,
    )


def _approval_kwargs(governance, change_set: dict, test_run: dict) -> dict:
    candidate = str(change_set["candidate_commit_sha"])
    diff = governance.change_set_diff(change_set, candidate)
    assert diff is not None
    review = inspect_candidate_review(
        diff,
        lambda path: governance.change_set_file_diff(change_set, candidate, path),
    )
    return {
        "candidate_commit_sha": candidate,
        "diff_digest": candidate_diff_digest(diff),
        "test_run_id": str(test_run["test_run_id"]),
        "suite_digest": str(test_run["suite_digest"]),
        "reviewed_files": [item.to_payload() for item in review.files],
    }


def _record_runtime_run(store, *, run_id: str, agent_id: str) -> None:
    timestamp = "2026-06-12T00:00:00Z"
    store.record_run(
        {
            "run_id": run_id,
            "session_id": f"session-{run_id}",
            "agent_id": agent_id,
            "agent_version_id": "a" * 40,
            "runtime_agent_id": f"runtime-{agent_id}",
            "harness_digest": "b" * 64,
            "status": "succeeded",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )


def _runnable_agent_test_source() -> str:
    return (
        "def test_agent(agent):\n"
        "    result = agent.run('仅依据以下已给定事实回答，不调用任何工具或读取文件。回答必须包含测试通过。')\n"
        "    assert not result.errors\n"
        "    normalized_text = ''.join(result.text.split())\n"
        "    assert '测试通过' in normalized_text\n"
        "    assert result.raw['agent_activity']['tool_calls'] == []\n"
    )


def _write_runnable_agent_test(worktree: Path) -> None:
    tests_dir = worktree / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("README.md").write_text("# Agent tests\n", encoding="utf-8")
    tests_dir.joinpath("test_agent.py").write_text(_runnable_agent_test_source(), encoding="utf-8")


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


def test_hitl_observation_is_derived_from_current_agent_manifest(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    manifest_path = workspace / "agent.yaml"
    manifest_path.write_text(
        "session:\n  permission_mode: default\nworkspace_policy:\n  ask_tools: [ReviewAction]\n",
        encoding="utf-8",
    )
    store.create_business_agent(
        name="SOC",
        agent_id="soc-ops",
        workspace_dir=str(workspace),
    )

    assert store.get_agent("soc-ops").requires_web_hitl is True

    manifest_path.write_text("session:\n  permission_mode: dont_ask\n", encoding="utf-8")
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


def test_lifespan_syncs_discovered_business_agent_registry(process_environment, tmp_path: Path) -> None:
    """应用启动（lifespan）幂等登记业务 Agent，使注册表在运行态被真实消费。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app):
        pass

    agents = module.agent_registry_store.list_agents()
    assert {agent.agent_id for agent in agents} == {ORDINARY_TEST_AGENT_ID, DEFAULT_BUSINESS_AGENT_ID}
    assert all(agent.category == "business" for agent in agents)


def test_list_agents_endpoint_returns_registered_business_agents(process_environment, tmp_path: Path) -> None:
    """AGV-004/007：注册的业务 Agent 定义可经 API 查询，作为外部接入与归属对象的可见入口。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        response = client.get("/api/agent-registry")

    assert response.status_code == 200
    body = response.json()
    assert {item["agent_id"] for item in body} == {ORDINARY_TEST_AGENT_ID, DEFAULT_BUSINESS_AGENT_ID}
    assert all(item["category"] == "business" for item in body)
    assert all(item["workspace_dir"] for item in body)
    assert all(item["agent_version_id"] is None for item in body)
    assert all(item["harness_digest"] is None for item in body)
    assert all(item["runtime_agent_id"] is None and item["provisioned"] is False for item in body)
    # Registry 盘点是纯读路径：不得顺带初始化 Git 或生成不可变 Runtime 快照。
    assert all(not Path(item["workspace_dir"]).joinpath(".git").exists() for item in body)
    default = next(item for item in body if item["agent_id"] == DEFAULT_BUSINESS_AGENT_ID)
    assert default["builtin"] is True and default["default"] is True and default["protected"] is True


def test_direct_create_and_template_catalog_endpoints_are_removed(process_environment, tmp_path: Path) -> None:
    """新 Agent 只能通过 Workspace 包导入，旧创建入口和模板 catalog 不保留兼容层。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        direct_create = client.post("/api/agent-registry", json={"name": "客服助手", "agent_id": "soc-ops"})
        template_catalog = client.get("/api/agent-registry/templates")

    assert direct_create.status_code == 405
    assert template_catalog.status_code == 405


def test_runtime_session_identity_resolves_real_published_binding(process_environment, tmp_path: Path) -> None:
    """Session 创建前置校验只接受当前不可变版本对应的 Runtime Agent 身份。"""
    from app.runtime_gateway.store import RuntimeObjectNotFound, harness_digest

    module = _load_app(process_environment, tmp_path)
    _seed_active_agent(
        module,
        agent_id="soc-ops",
        name="客服助手",
        requires_web_hitl=False,
    )
    with TestClient(module.app):
        pass
    record = module.agent_registry_store.get_agent("soc-ops")
    assert record is not None
    version_store = module.agent_governance._store_for("soc-ops")
    commit_sha = str(version_store.current_commit_sha())
    digest = harness_digest(Path(record.workspace_dir))
    snapshot = module.harness_snapshots.materialize(
        version_store=version_store,
        agent_id="soc-ops",
        agent_version_id=commit_sha,
        expected_digest=digest,
    )
    runtime_agent_id = "runtime-soc-ops-published"
    module.run_store.bind_agent_version(
        agent_id="soc-ops",
        agent_version_id=commit_sha,
        digest=digest,
        runtime_agent_id=runtime_agent_id,
        governance_agent_id="soc-ops",
        source_kind="published",
        source_id=snapshot.source_id,
    )

    binding = module.provisioner.require_current_runtime(runtime_agent_id)
    assert binding.agent_id == "soc-ops"
    assert binding.agent_version_id == commit_sha
    assert binding.harness_digest == digest
    assert binding.workspace_id == snapshot.workspace_id
    assert record.workspace_dir.endswith("/business-agents/soc-ops/workspace")
    with pytest.raises(RuntimeObjectNotFound):
        module.provisioner.require_current_runtime("soc-ops")


def test_workspace_import_creates_draft_while_existing_agents_remain_active(process_environment, tmp_path: Path) -> None:
    """AGV-020：新导入 Agent 在发布前为 draft，既有 Agent 仍按迁移默认值 active。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="soc-ops", name="客服助手")
        assert created.status_code == 200
        assert created.json()["agent"]["status"] == "draft"
        listed = {a["agent_id"]: a["status"] for a in client.get("/api/agent-registry").json()}
        assert listed["soc-ops"] == "draft"
        assert listed[DEFAULT_BUSINESS_AGENT_ID] == "active"
        assert listed[ORDINARY_TEST_AGENT_ID] == "active"


def test_feedback_asset_provenance_traces_agent_and_relationship(process_environment, tmp_path: Path) -> None:
    """AGV-022：从某次反馈可追溯资产关系——影响了哪个 Agent、改了哪些资产、进入哪个版本。"""
    from app.runtime.schemas import FeedbackEventIngestRequest, FeedbackSignalCreateRequest

    module = _load_app(process_environment, tmp_path)
    fs = module.feedback_store
    module.agent_registry_store.create_business_agent(
        name="SOC Ops",
        agent_id="soc-ops",
        workspace_dir=str(module.settings.data_dir / "business-agents" / "soc-ops" / "workspace"),
    )
    _record_runtime_run(fs, run_id="run-x", agent_id="soc-ops")
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
    event = fs.ingest_feedback_event(
        FeedbackEventIngestRequest(
            event_id="event-only-provenance",
            source_system="document-review",
            event_type="document.annotation.corrected",
            timestamp="2026-09-15T00:00:00Z",
            run_id="run-x",
        )
    )
    assert event.event.agent_id == "soc-ops"
    event_case = fs.create_case(source_refs=[("event", event.event.event_id)], title="事件来源归属")
    assert event_case is not None

    with TestClient(module.app) as client:
        prov = client.get(f"/api/asset-registry/feedback/{case_id}")
        event_prov = client.get(f"/api/asset-registry/feedback/{event_case['feedback_case_id']}")
        assert prov.status_code == 200
        assert event_prov.status_code == 200
        body = prov.json()
        assert body["feedback_case_id"] == case_id
        # 影响了哪个 Agent：从反馈归属可追溯。
        assert "soc-ops" in body["agent_ids"]
        assert body["improvements"][0]["improvement_id"] == improvement.improvement_id
        assert body["improvements"][0]["change_set_ids"] == ["agc-test"]
        assert event_prov.json()["agent_ids"] == ["soc-ops"]
        # 未知 case -> 404。
        assert client.get("/api/asset-registry/feedback/nope").status_code == 404


def test_business_agent_lifecycle_transitions_and_archived_excluded_from_run(process_environment, tmp_path: Path) -> None:
    """AGV-020：合法生命周期转移被接受，非法转移被拒且归档 Agent 停止计划任务。"""
    module = _load_app(process_environment, tmp_path)
    _seed_active_agent(module, agent_id="soc-ops", name="客服助手")
    with TestClient(module.app) as client:
        module.agent_test_schedule_service.update_schedule(
            "soc-ops",
            enabled=True,
            cron_expression="0 2 * * *",
            timezone_name="UTC",
        )
        # 合法转移：active -> deprecated -> archived。
        assert client.post("/api/agent-registry/soc-ops/lifecycle", json={"status": "deprecated"}).status_code == 200
        assert module.agent_test_schedule_store.get_schedule("soc-ops")["enabled"] is True
        archived = client.post("/api/agent-registry/soc-ops/lifecycle", json={"status": "archived"})
        assert archived.status_code == 200 and archived.json()["status"] == "archived"
        assert module.agent_test_schedule_store.get_schedule("soc-ops")["enabled"] is False
        # 非法转移：archived 为终态，archived -> active 被拒绝并返回可理解的状态机错误（409）。
        rejected = client.post("/api/agent-registry/soc-ops/lifecycle", json={"status": "active"})
        assert rejected.status_code == 409
        assert client.post("/api/agent-registry/soc-ops/lifecycle", json={"status": "unknown"}).status_code == 422
        assert "transition" in rejected.json()["detail"].lower()
        # archived Agent 仍可查询，保留审计身份。
        assert any(a["agent_id"] == "soc-ops" for a in client.get("/api/agent-registry").json())
        # 普通业务 Agent 与导入 Agent 使用同一生命周期状态机。
        ordinary_lifecycle = client.post(
            f"/api/agent-registry/{ORDINARY_TEST_AGENT_ID}/lifecycle",
            json={"status": "archived"},
        )
        assert ordinary_lifecycle.status_code == 200


def test_delete_business_agent_reports_impact_and_protects_builtin_agent(process_environment, tmp_path: Path) -> None:
    """AGV-031：统一入口下 agent_id 在运行、反馈、测试和版本中一致。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(process_environment, tmp_path)
    _seed_active_agent(module, agent_id="soc-ops", name="客服助手")
    fs = module.feedback_store
    gov = module.agent_governance
    with TestClient(module.app) as client:
        # 已发布治理对象的运行、反馈、测试和版本都用同一 agent_id 串联。
        _record_runtime_run(fs, run_id="run-x", agent_id="soc-ops")
        signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-x", labels=["tool_data_incomplete"]))
        # 版本维度：该 Agent 独立 change set → release（落自己的版本 store）。
        change_set = gov.create_change_set(title="soc-ops 候选", operator="t", agent_id="soc-ops")
        worktree = Path(str(change_set["worktree_path"]))
        # 仅变更测试资产，使此宿主删除事务用例保持非敏感候选；人工强制发布有明确例外标签。
        _write_runnable_agent_test(worktree)
        commit = gov._store_for("soc-ops").commit_worktree(worktree, message="c")
        change_set = gov.mark_candidate_committed(
            str(change_set["change_set_id"]),
            candidate_commit_sha=commit,
            execution_job_id=None,
            operator="t",
        )
        test_run = _record_unrun_test_run(
            module,
            agent_id="soc-ops",
            commit_sha=commit,
            change_set_id=str(change_set["change_set_id"]),
        )
        assert change_set["status"] == "candidate_committed"
        assert test_run["status"] == "queued"
        release = gov.publish_change_set(
            str(change_set["change_set_id"]),
            operator="t",
            force=True,
            note="仅验证宿主删除事务与跨实体影响面，不作为真实候选测试或正式发布验收",
        )
        assert release["force_published"] is True

        # Agent ID 在运行、反馈、测试和版本中保持一致。
        assert signal["agent_id"] == "soc-ops"
        assert test_run["agent_id"] == "soc-ops"
        assert change_set["agent_id"] == "soc-ops" and release["agent_id"] == "soc-ops"
        schedule = module.agent_test_schedule_service.update_schedule(
            "soc-ops",
            enabled=True,
            cron_expression="0 2 * * *",
            timezone_name="UTC",
        )
        assert schedule["enabled"] is True

        deleted = client.delete("/api/agent-registry/soc-ops")
        assert deleted.status_code == 200
        body = deleted.json()
        assert body["deleted"]["agent_id"] == "soc-ops"
        # 删除前给出跨维度影响面提示：运行、反馈、测试和版本均计入。
        impact = body["impact"]
        assert impact["runs"] >= 1 and impact["feedback_signals"] >= 1
        assert impact["test_runs"] >= 1
        assert impact["change_sets"] >= 1 and impact["releases"] >= 1
        retained_schedule = module.agent_test_schedule_store.get_schedule("soc-ops")
        assert retained_schedule is not None
        assert retained_schedule["enabled"] is False
        assert retained_schedule["next_run_at"] is None
        # 删除后不再出现在注册表。
        assert "soc-ops" not in {a["agent_id"] for a in client.get("/api/agent-registry").json()}
        # 受保护的内置 Agent 不可删（400）；未知 agent_id 报 404。
        assert client.delete(f"/api/agent-registry/{SECURITY_OPERATIONS_EXPERT_AGENT_ID}").status_code == 400
        assert client.delete("/api/agent-registry/biz-unknown").status_code == 404


def test_workspace_imported_business_agents_share_governance_without_builtin_special_cases(process_environment, tmp_path: Path) -> None:
    """AGV-044：Workspace 包接入的 Agent 与内置 Agent 复用同一治理抽象并保持隔离。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest

    module = _load_app(process_environment, tmp_path)
    fs = module.feedback_store
    gov = module.agent_governance
    with TestClient(module.app) as client:
        assert DEFAULT_BUSINESS_AGENT_ID in {a["agent_id"] for a in client.get("/api/agent-registry").json()}
        # 通过唯一创建入口接入一个新业务 Agent。
        imported = _import_new_agent(
            client,
            agent_id="shop-bot",
            name="电商助手",
            package=_workspace_package(
                {
                    "AGENT.md": b"# shop-bot\n",
                    "agent.yaml": _agent_manifest("shop-bot", requires_web_hitl=True),
                    "tests/README.md": b"# Agent tests\n",
                    "tests/test_agent.py": _runnable_agent_test_source().encode(),
                },
            ),
        )
        assert imported.status_code == 200
        imported_body = imported.json()
        assert imported_body["published"] is False
        assert imported_body["agent"]["status"] == "draft"
        assert "shop-bot" in {a["agent_id"] for a in client.get("/api/agent-registry").json()}

        # 复用运行、反馈、测试和版本能力，全部经 agent_id 归属。
        _record_runtime_run(fs, run_id="run-s", agent_id="shop-bot")
        signal = fs.create_signal(FeedbackSignalCreateRequest(run_id="run-s", labels=["tool_data_incomplete"]))
        change_set = gov.get_change_set(str(imported_body["change_set_id"]))
        assert change_set is not None
        commit = str(imported_body["candidate_commit_sha"])
        test_run = _record_unrun_test_run(
            module,
            agent_id="shop-bot",
            commit_sha=commit,
            change_set_id=str(change_set["change_set_id"]),
        )
        assert change_set["status"] == "pending_approval"
        assert test_run["status"] == "queued"
        with pytest.raises(AgentGovernanceError, match="候选审批前必须完成且通过精确 candidate commit 的平台测试") as approval_rejected:
            gov.approve_change_set(
                str(change_set["change_set_id"]),
                operator="reviewer",
                **_approval_kwargs(gov, change_set, test_run),
            )
        assert approval_rejected.value.status_code == 409
        with pytest.raises(AgentGovernanceError, match="require explicit manual approval before publication") as force_rejected:
            gov.publish_change_set(
                str(change_set["change_set_id"]),
                operator="t",
                force=True,
                note="宿主测试不得绕过敏感候选审批",
            )
        assert force_rejected.value.status_code == 409

        # 同一抽象、不同实例：内置 Agent 与新 Agent 的版本 store 物理隔离。
        assert gov._store_for("shop-bot") is not gov._store_for(DEFAULT_BUSINESS_AGENT_ID)
        builtin_head = gov._store_for(DEFAULT_BUSINESS_AGENT_ID).current_commit_sha()
        # 反馈、版本和测试按 Agent 维度隔离；未获真实测试证据时双方版本链均不变。
        assert signal["agent_id"] == "shop-bot" and test_run["agent_id"] == "shop-bot"
        shop_cs = {c["change_set_id"] for c in gov.list_change_sets(agent_id="shop-bot")}
        assert shop_cs == {change_set["change_set_id"]}
        assert shop_cs.isdisjoint({c["change_set_id"] for c in gov.list_change_sets(agent_id=DEFAULT_BUSINESS_AGENT_ID)})
        assert gov.get_change_set(str(change_set["change_set_id"]))["status"] == "pending_approval"
        assert gov.list_releases(agent_id="shop-bot") == []
        assert gov._store_for("shop-bot").current_commit_sha() != commit
        assert gov._store_for(DEFAULT_BUSINESS_AGENT_ID).current_commit_sha() == builtin_head


def _settings_with_data_dir(process_environment, tmp_path: Path) -> AppSettings:
    """构造 data_dir 指向 tmp 的设置，用于隔离发现逻辑的磁盘扫描。"""
    process_environment.set("DATA_DIR", str(tmp_path / "data"))
    return AppSettings(_env_file=None, AGENTGOV_RUNTIME_SHARED_SECRET="test-runtime-shared-secret")


def test_discover_business_agents_finds_all_live_workspaces(process_environment, tmp_path: Path) -> None:
    """运行态存在多个业务 Agent Workspace 时，发现逻辑识别全部。"""
    settings = _settings_with_data_dir(process_environment, tmp_path)
    for agent_id in (ORDINARY_TEST_AGENT_ID, "AAA", "BBB"):
        business_agent_layout(settings.data_dir, agent_id).workspace.mkdir(parents=True, exist_ok=True)

    discovered = {profile.name: profile for profile in discover_business_agents(settings)}

    assert set(discovered) == {ORDINARY_TEST_AGENT_ID, "AAA", "BBB"}
    aaa = discovered["AAA"]
    # 全部走同一抽象：category=business、name=agent_id、workspace_dir 由 layout 单一真相派生。
    assert aaa.category == "business"
    assert aaa.name == "AAA"
    assert str(aaa.workspace_dir).endswith("/business-agents/AAA/workspace")


def test_discover_returns_empty_when_business_agents_root_absent(process_environment, tmp_path: Path) -> None:
    """运行卷尚未 bootstrap（business-agents 目录不存在）时发现逻辑安全返回空，不抛错。"""
    settings = _settings_with_data_dir(process_environment, tmp_path)
    assert discover_business_agents(settings) == []


def test_discover_skips_invalid_and_non_agent_entries(process_environment, tmp_path: Path) -> None:
    """③ 外部输入（磁盘目录名）异常/越权：非法 agent_id、缺 workspace、非目录条目一律跳过，不污染注册表。"""
    settings = _settings_with_data_dir(process_environment, tmp_path)
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


def test_lifespan_auto_registers_live_business_agent_workspaces(process_environment, tmp_path: Path) -> None:
    """端到端：磁盘上的 AAA/BBB Workspace 在应用启动时被登记。"""
    module = _load_app(process_environment, tmp_path)
    data_dir = module.settings.data_dir
    # 模拟外部导入已落盘：把两个业务 Agent 的 Workspace 放入运行卷。
    for agent_id in ("AAA", "BBB"):
        ws = business_agent_layout(data_dir, agent_id).workspace
        create_test_business_agent_workspace(ws, agent_id=agent_id, name=agent_id)
    with TestClient(module.app) as client:
        listed = client.get("/api/agent-registry").json()
        ids = {a["agent_id"] for a in listed}
        # 磁盘上的 AAA/BBB 与夹具已有业务 Agent 一同进入注册表。
        assert {DEFAULT_BUSINESS_AGENT_ID, ORDINARY_TEST_AGENT_ID, "AAA", "BBB"} <= ids
        discovered = next(a for a in listed if a["agent_id"] == "AAA")
        assert discovered["category"] == "business"
        assert discovered["status"] == "active"
        assert discovered["workspace_dir"].endswith("/business-agents/AAA/workspace")


def test_lifespan_discovery_keeps_each_business_agent_single_row_across_restarts(process_environment, tmp_path: Path) -> None:
    """重复启动 sync 不得为已发现业务 Agent 产生重复记录。"""
    module = _load_app(process_environment, tmp_path)
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
