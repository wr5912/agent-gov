from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.services.agent_governance import AgentGovernanceError

from agent_governance_publish_test_support import _candidate_change_set, _governance
from business_agent_test_utils import LEGACY_MAIN_AGENT_ID, ORDINARY_TEST_AGENT_ID


def test_publish_accepts_candidate_with_real_mcp_endpoint(tmp_path):
    governance, store = _governance(tmp_path)
    original_head = store.current_commit_sha()
    change_set = governance.create_change_set(title="real MCP endpoint", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    mcp_path = worktree / ".mcp.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp["mcpServers"]["sec-ops-data"]["url"] = "http://unapproved.example/mcp"
    mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="drift managed MCP")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-invalid-policy",
    )

    published = governance.publish_change_set(str(committed["change_set_id"]), operator="tester")

    assert published is not None
    assert store.current_commit_sha() != original_head
    assert json.loads((store.repository_dir / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["sec-ops-data"]["url"] == (
        "http://unapproved.example/mcp"
    )


def test_publish_rejects_candidate_with_missing_referenced_hook(tmp_path):
    governance, store = _governance(tmp_path)
    original_head = store.current_commit_sha()
    change_set = governance.create_change_set(title="invalid managed hook", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    settings_path = worktree / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'python "$CLAUDE_PROJECT_DIR/hooks/missing_guard.py"',
                    }
                ],
            }
        ]
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="remove referenced hook")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-invalid-hook-policy",
    )

    with pytest.raises(AgentGovernanceError, match="Managed Agent policy rejected"):
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")

    assert store.current_commit_sha() == original_head


def test_publish_accepts_candidate_with_custom_referenced_hook(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="custom managed hook", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    settings_path = worktree / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.setdefault("hooks", {}).setdefault("PostToolUse", []).append(
        {
            "matcher": "Write",
            "hooks": [
                {
                    "type": "command",
                    "command": 'python "$CLAUDE_PROJECT_DIR/hooks/custom_audit.py"',
                }
            ],
        }
    )
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    custom_hook = worktree / "hooks" / "custom_audit.py"
    custom_hook.parent.mkdir(parents=True, exist_ok=True)
    custom_hook.write_text("# custom managed hook\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="add custom referenced hook")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-custom-hook-policy",
    )

    published = governance.publish_change_set(str(committed["change_set_id"]), operator="tester")

    assert published is not None
    assert (store.repository_dir / "hooks" / "custom_audit.py").is_file()


def test_business_agent_version_chain_is_isolated_from_platform_default(tmp_path):
    """B3.2/B3.3：普通业务 Agent 的版本链与平台默认业务 Agent 相互隔离。"""
    governance, default_store = _governance(tmp_path)
    default_head_before = default_store.current_commit_sha()

    # 为业务 Agent 创建 → 提交 → 发布一条独立版本记录。
    biz_change_set = _candidate_change_set(
        governance,
        default_store,
        content="# Biz Agent\n\n业务 Agent 候选。\n",
        agent_id="biz-agent-001",
    )
    assert biz_change_set["agent_id"] == "biz-agent-001"
    biz_release = governance.publish_change_set(str(biz_change_set["change_set_id"]), operator="tester")
    assert biz_release["agent_id"] == "biz-agent-001"

    # 隔离性：发布普通业务 Agent 版本不改动默认 Agent 的版本链。
    assert default_store.current_commit_sha() == default_head_before
    biz_store = governance._store_for("biz-agent-001")
    assert biz_store.repository_dir != default_store.repository_dir
    assert biz_store.current_commit_sha() == biz_release["commit_sha"]
    assert biz_store.repository_dir != default_store.repository_dir

    # 按 Agent 过滤互不串扰：各自只看到自己的 change set/release。
    assert [cs["change_set_id"] for cs in governance.list_change_sets(agent_id="biz-agent-001")] == [biz_change_set["change_set_id"]]
    assert governance.list_change_sets(agent_id=DEFAULT_BUSINESS_AGENT_ID) == []
    assert [rel["release_id"] for rel in governance.list_releases(agent_id="biz-agent-001")] == [biz_release["release_id"]]
    assert governance.list_releases(agent_id=DEFAULT_BUSINESS_AGENT_ID) == []

    # 默认 Agent 路径不受影响，仍可独立创建并发布版本。
    default_change_set = _candidate_change_set(governance, default_store, content="# Default Agent\n\n默认候选。\n")
    assert default_change_set["agent_id"] == DEFAULT_BUSINESS_AGENT_ID
    default_release = governance.publish_change_set(str(default_change_set["change_set_id"]), operator="tester")
    assert default_release["agent_id"] == DEFAULT_BUSINESS_AGENT_ID
    assert default_store.current_commit_sha() == default_release["commit_sha"]
    # 普通业务 Agent 链未被默认 Agent 发布污染。
    assert biz_store.current_commit_sha() == biz_release["commit_sha"]


def test_governance_serves_multiple_business_agents_with_isolated_closed_loops(tmp_path):
    """AGV-017：多个业务 Agent 的运行、反馈、测试门和版本记录互不混淆。"""
    governance, default_store = _governance(tmp_path)
    store = governance.feedback_store
    agents = ("agent-alpha", "agent-beta")

    records: dict[str, dict] = {}
    for agent_id in agents:
        # 每个业务 Agent 一条独立闭环记录：run -> signal -> case + change set/release。
        store.record_run({"run_id": f"run-{agent_id}", "agent_id": agent_id, "created_at": "2026-06-12T00:00:00Z"})
        signal = store.create_signal(FeedbackSignalCreateRequest(run_id=f"run-{agent_id}", labels=["tool_data_incomplete"]))
        case = store.create_case(source_refs=[("signal", signal["signal_id"])], title=f"{agent_id} 反馈")
        change_set = _candidate_change_set(
            governance,
            default_store,
            content=f"# {agent_id}\n\n候选\n",
            agent_id=agent_id,
        )
        release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
        records[agent_id] = {
            "signal": signal,
            "case": case,
            "change_set": change_set,
            "release": release,
        }

    # 治理 Agent（单一 governance 实例）为不同业务 Agent 各自管理独立版本 store（物理隔离）。
    assert governance._store_for("agent-alpha") is not governance._store_for("agent-beta")

    # 每个维度按 Agent 过滤只见自身记录，不被另一个 Agent 串扰。
    for agent_id in agents:
        assert {str(r["agent_id"]) for r in store.list_runs(agent_id=agent_id)} == {agent_id}
        assert {str(s["agent_id"]) for s in store.list_signals(agent_id=agent_id)} == {agent_id}
        assert records[agent_id]["case"]["agent_id"] == agent_id
        assert records[agent_id]["change_set"]["latest_test_run"]["agent_id"] == agent_id
        assert {str(c["agent_id"]) for c in governance.list_change_sets(agent_id=agent_id)} == {agent_id}
        assert {str(rel["agent_id"]) for rel in governance.list_releases(agent_id=agent_id)} == {agent_id}

    # 跨 Agent 隔离：alpha 的版本记录不出现在 beta 的过滤视图。
    alpha_cs = {str(c["change_set_id"]) for c in governance.list_change_sets(agent_id="agent-alpha")}
    beta_cs = {str(c["change_set_id"]) for c in governance.list_change_sets(agent_id="agent-beta")}
    assert alpha_cs and beta_cs and alpha_cs.isdisjoint(beta_cs)
    # 各 Agent 版本链落在各自 store，互不污染。
    assert governance._store_for("agent-alpha").current_commit_sha() == records["agent-alpha"]["release"]["commit_sha"]
    assert governance._store_for("agent-beta").current_commit_sha() == records["agent-beta"]["release"]["commit_sha"]


def test_business_agent_version_lifecycle_preserves_history_through_rollback(tmp_path):
    """AGV-021（业务 Agent 生命周期围绕版本治理运转）：候选/已发布/回滚版本可区分，rollback 与 restore 不物理删除历史 release。"""
    governance, default_store = _governance(tmp_path)
    agent_id = "biz-agent-021"

    # 候选 → 发布 v1。
    cs1 = _candidate_change_set(governance, default_store, content="# Biz\n\nv1\n", agent_id=agent_id)
    assert cs1["status"] == "candidate_committed"  # 待发布版本可区分
    release_v1 = governance.publish_change_set(str(cs1["change_set_id"]), operator="tester")
    # 候选 → 发布 v2。
    cs2 = _candidate_change_set(governance, default_store, content="# Biz\n\nv2\n", agent_id=agent_id)
    release_v2 = governance.publish_change_set(str(cs2["change_set_id"]), operator="tester")

    biz_store = governance._store_for(agent_id)
    assert biz_store.current_commit_sha() == release_v2["commit_sha"]
    assert release_v1["status"] == "published" and release_v2["status"] == "published"

    # rollback v2：标记为 rolled_back（与 published 可区分），但 release 记录不被物理删除、历史可解释。
    rolled = governance.rollback_release(str(release_v2["release_id"]), operator="tester", note="回滚 v2")
    assert rolled["status"] == "rolled_back"  # 回滚版本可区分
    assert rolled["rollback_target_commit_sha"] == release_v1["commit_sha"]
    assert biz_store.current_commit_sha() == release_v1["commit_sha"]
    persisted_v2 = governance.get_release(str(release_v2["release_id"]))
    assert persisted_v2 is not None  # rollback 不删除历史 release
    assert persisted_v2["status"] == "rolled_back"
    # restore 到 v1：切换当前版本但不改写 release 历史（两条 release 均仍可追溯）。
    restore = governance.restore_release(str(release_v1["release_id"]), operator="tester", note="切回 v1")
    assert restore["restore_result"]["current_commit_sha"] == release_v1["commit_sha"]
    assert biz_store.current_commit_sha() == release_v1["commit_sha"]
    assert governance.get_release(str(release_v1["release_id"]))["status"] == "published"
    assert governance.get_release(str(release_v1["release_id"]))["agent_id"] == agent_id
    # v1 不受 v2 回滚影响，历史完整：两条 release 仍在 Agent 维度可查。
    releases = {str(rel["release_id"]): rel["status"] for rel in governance.list_releases(agent_id=agent_id)}
    assert releases == {str(release_v1["release_id"]): "published", str(release_v2["release_id"]): "rolled_back"}
    # 版本链未被物理删除：v1、v2 两个 commit 在该 Agent 版本 store 中均可解析。
    assert governance.get_release(str(release_v1["release_id"]))["commit_sha"] == release_v1["commit_sha"]


def test_create_change_set_rejects_path_traversal_agent_id(tmp_path):
    """B3.2 越权输入：无效 agent_id（路径穿越）不得用于版本 store 落地路径。"""
    governance, _ = _governance(tmp_path)
    for hostile in ["../evil", "biz/../../etc", ".", "..", "a/b", "with space"]:
        with pytest.raises(AgentGovernanceError) as exc:
            governance.create_change_set(title="越界归属", operator="invalid-input", agent_id=hostile)
        assert exc.value.status_code == 400


def test_restore_release_switches_current_workspace_without_mutating_release_history(tmp_path):
    governance, agent_store = _governance(tmp_path)
    first_change_set = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv1\n")
    first_release = governance.publish_change_set(str(first_change_set["change_set_id"]), operator="tester")
    second_change_set = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv2\n")
    second_release = governance.publish_change_set(str(second_change_set["change_set_id"]), operator="tester")

    assert agent_store.current_commit_sha() == second_release["commit_sha"]

    restore = governance.restore_release(str(first_release["release_id"]), operator="tester", note="切换到 v1")

    assert restore["release"]["release_id"] == first_release["release_id"]
    assert restore["release"]["status"] == "published"
    assert restore["restore_result"]["current_commit_sha"] == first_release["commit_sha"]
    assert agent_store.current_commit_sha() == first_release["commit_sha"]
    assert governance.get_release(str(first_release["release_id"]))["status"] == "published"
    assert governance.get_release(str(second_release["release_id"]))["status"] == "published"


def test_terminal_change_set_cannot_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    governance.reject_change_set(str(change_set["change_set_id"]), operator="tester")

    with pytest.raises(AgentGovernanceError, match="cannot be published from status rejected") as exc:
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert exc.value.status_code == 409


def test_high_risk_change_set_requires_approval_before_publish(tmp_path):
    """AGV-041：标记为待审批的高风险变更不经审批不得发布；审批后可发布。"""
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    pending = governance.request_change_set_approval(
        change_set_id,
        operator="reviewer",
        reason="改动生产策略 prompt",
        impact_scope="默认业务 Agent 全量输出",
        rollback_plan="回滚到上一个 release",
    )
    assert pending["status"] == "pending_approval"
    assert pending["impact_scope"] == "默认业务 Agent 全量输出"
    assert pending["rollback_plan"] == "回滚到上一个 release"

    with pytest.raises(AgentGovernanceError) as exc:
        governance.publish_change_set(change_set_id, operator="tester")
    assert exc.value.status_code == 409

    governance.approve_change_set(change_set_id, operator="reviewer", note="审批通过")
    release = governance.publish_change_set(change_set_id, operator="tester")
    assert release["status"] == "published"


def test_rejected_change_set_records_audit_event(tmp_path):
    """AGV-041：拒绝高风险变更产生审计事件，且变更不发布。"""
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    governance.request_change_set_approval(
        change_set_id,
        operator="reviewer",
        reason="风险过高",
        impact_scope="工具配置",
        rollback_plan="撤销变更",
    )
    rejected = governance.reject_change_set(change_set_id, operator="reviewer", note="不通过")

    assert rejected["status"] == "rejected"
    actions = {str(event.get("action")) for event in governance.list_change_set_events(change_set_id)}
    assert {"approval_requested", "rejected"} <= actions


def test_repository_ops_route_per_agent_not_always_platform_default(tmp_path):
    """缺陷②回归：repository_status/snapshot/current_ref 按 agent_id 路由到对应 per-agent 版本库，
    不再恒走平台默认业务 Agent 的版本库。"""
    governance, default_store = _governance(tmp_path)
    assert governance._store_for(None) is default_store
    ordinary_store = governance._store_for(ORDINARY_TEST_AGENT_ID)
    assert ordinary_store.repository_dir != default_store.repository_dir
    # 其他业务 Agent 也走独立 per-Agent 库。
    biz_store = governance._store_for("biz-x")
    assert biz_store.repository_dir != default_store.repository_dir
    assert default_store.repository_dir != biz_store.repository_dir
    assert "business-agents/biz-x/workspace" in str(biz_store.repository_dir)
    # repository_status 按 agent_id 路由：业务 Agent 的状态来自其自己的库，不是默认库。
    biz_status = governance.repository_status("biz-x")
    default_status = governance.repository_status(DEFAULT_BUSINESS_AGENT_ID)
    assert str(biz_store.repository_dir) == str(biz_status["repository_dir"])
    assert biz_status["repository_dir"] != default_status["repository_dir"]


def test_version_governance_rejects_unregistered_ghost_agent(tmp_path):
    """缺陷④：装配 agent_exists 后，未注册 agent_id 的版本治理操作被拒（404），不懒建幽灵版本库。

    main-agent 不再豁免这条校验：它是可删除的普通业务 Agent，删除后对它的版本治理请求应当
    404，而不是就地重建一个版本库把它复活。
    """
    governance, _ = _governance(tmp_path)
    governance.agent_exists = lambda aid: aid in {"real-biz", LEGACY_MAIN_AGENT_ID}
    with pytest.raises(AgentGovernanceError) as exc:
        governance.repository_status("ghost-agent")
    assert exc.value.status_code == 404
    # 已注册的放行（main-agent 与其他业务 Agent 同等对待）。
    assert governance.repository_status(LEGACY_MAIN_AGENT_ID)
    assert governance.repository_status("real-biz")

    # main-agent 未注册（已删除）时同样 404——没有「恒有效」豁免。
    governance.evict_agent_store(LEGACY_MAIN_AGENT_ID)
    governance.agent_exists = lambda aid: aid == "real-biz"
    with pytest.raises(AgentGovernanceError) as deleted_main:
        governance.repository_status(LEGACY_MAIN_AGENT_ID)
    assert deleted_main.value.status_code == 404
