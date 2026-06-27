from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService

from feedback_store_test_utils import _settings


def _governance(tmp_path):
    settings = _settings(tmp_path)
    agent_store = GitAgentVersionStore(
        repository_dir=settings.main_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    agent_store.ensure_bootstrap()
    store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.main_workspace_dir,
        agent_version_provider=lambda _aid=None: agent_store.current_version_id(),
    )
    return AgentGovernanceService(feedback_store=store, agent_version_store=agent_store), agent_store


def _candidate_change_set(
    governance: AgentGovernanceService,
    agent_store: GitAgentVersionStore,
    *,
    content: str = "# Test Agent\n\n发布候选变更。\n",
    agent_id: str | None = None,
):
    change_set = governance.create_change_set(title="候选发布测试", operator="tester", agent_id=agent_id)
    worktree_path = Path(str(change_set["worktree_path"]))
    worktree_path.joinpath("CLAUDE.md").write_text(content, encoding="utf-8")
    # 候选提交必须落在该 change set 归属 Agent 自己的版本 store（per-agent 隔离）。
    commit_store = governance._store_for(change_set.get("agent_id"))
    candidate_commit = commit_store.commit_worktree(worktree_path, message="Commit candidate change")
    return governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate_commit,
        execution_job_id="job-publish-test",
        operator="tester",
    )


def test_change_set_and_release_carry_agent_id_and_filter(tmp_path):
    """B3.1（AGV-017 版本维度基础）：change set/release 带 agent_id（默认 main-agent）且可按 Agent 过滤。"""
    governance, agent_store = _governance(tmp_path)
    candidate = _candidate_change_set(governance, agent_store)
    # change set 带 agent_id，默认归 main-agent（main 路径不变）。
    assert candidate["agent_id"] == "main-agent"
    assert all(cs["agent_id"] == "main-agent" for cs in governance.list_change_sets())
    # 按 Agent 维度过滤 change set：main 命中、其他 Agent 为空（不串扰）。
    assert governance.list_change_sets(agent_id="main-agent")
    assert governance.list_change_sets(agent_id="biz-other") == []
    # 发布后 release 同样带 agent_id 且可按 Agent 过滤。
    published = governance.publish_change_set(str(candidate["change_set_id"]), operator="tester")
    assert published is not None
    assert all(rel["agent_id"] == "main-agent" for rel in governance.list_releases())
    assert governance.list_releases(agent_id="main-agent")
    assert governance.list_releases(agent_id="biz-other") == []


def test_business_agent_version_chain_is_isolated_from_main(tmp_path):
    """B3.2/B3.3（AGV-017 per-agent 版本隔离）：业务 Agent 的 change set/release 落在自己独立的版本 store，与 main 互不混淆。"""
    governance, main_store = _governance(tmp_path)
    main_head_before = main_store.current_commit_sha()

    # 为业务 Agent 创建 → 提交 → 发布一条独立版本记录。
    biz_change_set = _candidate_change_set(
        governance, main_store, content="# Biz Agent\n\n业务 Agent 候选。\n", agent_id="biz-agent-001"
    )
    assert biz_change_set["agent_id"] == "biz-agent-001"
    biz_release = governance.publish_change_set(str(biz_change_set["change_set_id"]), operator="tester")
    assert biz_release["agent_id"] == "biz-agent-001"

    # 隔离性：发布业务 Agent 版本不改动 main 版本链（main HEAD 不变）。
    assert main_store.current_commit_sha() == main_head_before
    biz_store = governance._store_for("biz-agent-001")
    assert biz_store is not main_store
    assert biz_store.current_commit_sha() == biz_release["commit_sha"]
    assert biz_store.repository_dir != main_store.repository_dir

    # 按 Agent 过滤互不串扰：各自只看到自己的 change set/release。
    assert [cs["change_set_id"] for cs in governance.list_change_sets(agent_id="biz-agent-001")] == [
        biz_change_set["change_set_id"]
    ]
    assert governance.list_change_sets(agent_id="main-agent") == []
    assert [rel["release_id"] for rel in governance.list_releases(agent_id="biz-agent-001")] == [
        biz_release["release_id"]
    ]
    assert governance.list_releases(agent_id="main-agent") == []

    # main 路径不受影响：仍可独立创建并发布 main 版本，且与业务 Agent 链不混淆。
    main_change_set = _candidate_change_set(governance, main_store, content="# Main Agent\n\nmain 候选。\n")
    assert main_change_set["agent_id"] == "main-agent"
    main_release = governance.publish_change_set(str(main_change_set["change_set_id"]), operator="tester")
    assert main_release["agent_id"] == "main-agent"
    assert main_store.current_commit_sha() == main_release["commit_sha"]
    # 业务 Agent 链未被 main 发布污染。
    assert biz_store.current_commit_sha() == biz_release["commit_sha"]


def test_governance_serves_multiple_business_agents_with_isolated_closed_loops(tmp_path):
    """AGV-017：治理 Agent 服务多个业务 Agent，每个 Agent 的 run/反馈/优化/评估/版本记录互不混淆，可按 Agent 维度过滤。"""
    governance, main_store = _governance(tmp_path)
    store = governance.feedback_store
    agents = ("agent-alpha", "agent-beta")

    records: dict[str, dict] = {}
    for agent_id in agents:
        # 每个业务 Agent 一条独立闭环记录：run→signal→case→batch(优化) + change set/release(版本) + eval(评估)。
        store.record_run({"run_id": f"run-{agent_id}", "agent_id": agent_id, "created_at": "2026-06-12T00:00:00Z"})
        signal = store.create_signal(FeedbackSignalCreateRequest(run_id=f"run-{agent_id}", labels=["tool_data_incomplete"]))
        case = store.create_case(source_ids=[signal["signal_id"]], title=f"{agent_id} 反馈")
        batch = store.ensure_single_case_optimization_batch(case["feedback_case_id"])
        change_set = _candidate_change_set(governance, main_store, content=f"# {agent_id}\n\n候选\n", agent_id=agent_id)
        release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
        eval_run = store.create_eval_run(
            eval_case_ids=[], agent_version_id=release["commit_sha"], change_set_id=str(change_set["change_set_id"])
        )
        records[agent_id] = {"signal": signal, "batch": batch, "change_set": change_set, "release": release, "eval": eval_run}

    # 治理 Agent（单一 governance 实例）为不同业务 Agent 各自管理独立版本 store（物理隔离）。
    assert governance._store_for("agent-alpha") is not governance._store_for("agent-beta")

    # 每个维度按 Agent 过滤只见自身记录，不被另一个 Agent 串扰。
    for agent_id in agents:
        assert {r["agent_id"] for r in store.list_runs(agent_id=agent_id)} == {agent_id}
        assert {s["agent_id"] for s in store.list_signals(agent_id=agent_id)} == {agent_id}
        assert records[agent_id]["batch"]["agent_id"] == agent_id
        assert {e["agent_id"] for e in store.list_eval_runs(agent_id=agent_id)} == {agent_id}
        assert {c["agent_id"] for c in governance.list_change_sets(agent_id=agent_id)} == {agent_id}
        assert {rel["agent_id"] for rel in governance.list_releases(agent_id=agent_id)} == {agent_id}

    # 跨 Agent 隔离：alpha 的版本/评估记录不出现在 beta 的过滤视图。
    alpha_cs = {c["change_set_id"] for c in governance.list_change_sets(agent_id="agent-alpha")}
    beta_cs = {c["change_set_id"] for c in governance.list_change_sets(agent_id="agent-beta")}
    assert alpha_cs and beta_cs and alpha_cs.isdisjoint(beta_cs)
    alpha_evals = {e["eval_run_id"] for e in store.list_eval_runs(agent_id="agent-alpha")}
    beta_evals = {e["eval_run_id"] for e in store.list_eval_runs(agent_id="agent-beta")}
    assert alpha_evals and beta_evals and alpha_evals.isdisjoint(beta_evals)
    # 各 Agent 版本链落在各自 store，互不污染。
    assert governance._store_for("agent-alpha").current_commit_sha() == records["agent-alpha"]["release"]["commit_sha"]
    assert governance._store_for("agent-beta").current_commit_sha() == records["agent-beta"]["release"]["commit_sha"]


def test_business_agent_version_lifecycle_preserves_history_through_rollback(tmp_path):
    """AGV-021（业务 Agent 生命周期围绕版本治理运转）：候选/已发布/回滚版本可区分，rollback 与 restore 不物理删除历史 release。"""
    governance, main_store = _governance(tmp_path)
    agent_id = "biz-agent-021"

    # 候选 → 发布 v1。
    cs1 = _candidate_change_set(governance, main_store, content="# Biz\n\nv1\n", agent_id=agent_id)
    assert cs1["status"] == "candidate_committed"  # 候选版本可区分
    release_v1 = governance.publish_change_set(str(cs1["change_set_id"]), operator="tester")
    # 候选 → 发布 v2。
    cs2 = _candidate_change_set(governance, main_store, content="# Biz\n\nv2\n", agent_id=agent_id)
    release_v2 = governance.publish_change_set(str(cs2["change_set_id"]), operator="tester")

    biz_store = governance._store_for(agent_id)
    assert biz_store.current_commit_sha() == release_v2["commit_sha"]
    assert release_v1["status"] == "published" and release_v2["status"] == "published"

    # restore 到 v1：切换当前版本但不改写 release 历史（两条 release 均仍可追溯）。
    restore = governance.restore_release(str(release_v1["release_id"]), operator="tester", note="切回 v1")
    assert restore["restore_result"]["current_commit_sha"] == release_v1["commit_sha"]
    assert biz_store.current_commit_sha() == release_v1["commit_sha"]
    assert governance.get_release(str(release_v1["release_id"]))["status"] == "published"
    assert governance.get_release(str(release_v1["release_id"]))["agent_id"] == agent_id

    # rollback v2：标记为 rolled_back（与 published 可区分），但 release 记录不被物理删除、历史可解释。
    rolled = governance.rollback_release(str(release_v2["release_id"]), operator="tester", note="回滚 v2")
    assert rolled["status"] == "rolled_back"  # 回滚版本可区分
    persisted_v2 = governance.get_release(str(release_v2["release_id"]))
    assert persisted_v2 is not None  # rollback 不删除历史 release
    assert persisted_v2["status"] == "rolled_back"
    # v1 不受 v2 回滚影响，历史完整：两条 release 仍在 Agent 维度可查。
    releases = {rel["release_id"]: rel["status"] for rel in governance.list_releases(agent_id=agent_id)}
    assert releases == {release_v1["release_id"]: "published", release_v2["release_id"]: "rolled_back"}
    # 版本链未被物理删除：v1、v2 两个 commit 在该 Agent 版本 store 中均可解析。
    assert governance.get_release(str(release_v1["release_id"]))["commit_sha"] == release_v1["commit_sha"]


def test_create_change_set_rejects_path_traversal_agent_id(tmp_path):
    """B3.2 越权输入：恶意 agent_id（路径穿越）不得用于版本 store 落地路径。"""
    governance, _ = _governance(tmp_path)
    for hostile in ["../evil", "biz/../../etc", ".", "..", "a/b", "with space"]:
        with pytest.raises(AgentGovernanceError) as exc:
            governance.create_change_set(title="恶意归属", operator="attacker", agent_id=hostile)
        assert exc.value.status_code == 400


def test_candidate_committed_change_set_can_publish_directly(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)

    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert release["commit_sha"] == change_set["candidate_commit_sha"]
    assert release["status"] == "published"
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert governance.get_change_set(str(change_set["change_set_id"]))["status"] == "published"


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


def test_batch_regression_failed_cases_block_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    governance.mark_regression_running(str(change_set["change_set_id"]), eval_run_id="pending", operator="tester")
    regression_failed = governance.complete_regression(
        str(change_set["change_set_id"]),
        eval_run={
            "eval_run_id": "evr-batch-failed",
            "source": "optimization_batch_regression",
            "change_set_id": change_set["change_set_id"],
            "result_status": "passed_with_notes",
            "summary": {"total": 1, "passed": 0, "failed": 1, "needs_human_review": 0},
            "gate_result": {"status": "passed_with_notes", "note_case_ids": ["evc-failed"]},
            "items": [{"eval_case_id": "evc-failed", "status": "failed"}],
        },
        operator="tester",
    )

    assert regression_failed["status"] == "regression_failed"
    assert "批次回归存在失败用例" in regression_failed["publication_blocker"]
    with pytest.raises(AgentGovernanceError, match="批次回归存在失败用例") as exc:
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert exc.value.status_code == 409
    assert agent_store.current_commit_sha() != change_set["candidate_commit_sha"]


def test_force_publish_failed_regression_records_audit_event(tmp_path):
    """P4 发布门禁：普通发布被失败回归阻断；force=True 才能发布并留下强制审计。"""
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    governance.mark_regression_running(change_set_id, eval_run_id="pending", operator="tester")
    governance.complete_regression(
        change_set_id,
        eval_run={
            "eval_run_id": "evr-force-failed",
            "source": "optimization_batch_regression",
            "change_set_id": change_set_id,
            "result_status": "failed",
            "summary": {"total": 1, "passed": 0, "failed": 1, "needs_human_review": 0},
            "items": [{"eval_case_id": "evc-force-failed", "status": "failed"}],
        },
        operator="tester",
    )

    with pytest.raises(AgentGovernanceError):
        governance.publish_change_set(change_set_id, operator="tester")

    release = governance.publish_change_set(change_set_id, operator="lead", note="人工确认风险可接受", force=True)
    persisted = governance.get_change_set(change_set_id)

    assert release["status"] == "published"
    assert persisted["status"] == "published"
    assert persisted["force_published"] is True
    assert "批次回归存在失败用例" in persisted["force_publication_blocker"]
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert "force_published" in {str(event["action"]) for event in governance.list_change_set_events(change_set_id)}


def test_high_risk_change_set_requires_approval_before_publish(tmp_path):
    """AGV-041：标记为待审批的高风险变更不经审批不得发布；审批后可发布。"""
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    pending = governance.request_change_set_approval(
        change_set_id,
        operator="reviewer",
        reason="改动生产策略 prompt",
        impact_scope="main agent 全量输出",
        rollback_plan="回滚到上一个 release",
    )
    assert pending["status"] == "pending_approval"
    assert pending["impact_scope"] == "main agent 全量输出"
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
        change_set_id, operator="reviewer", reason="风险过高", impact_scope="工具配置", rollback_plan="撤销变更"
    )
    rejected = governance.reject_change_set(change_set_id, operator="reviewer", note="不通过")

    assert rejected["status"] == "rejected"
    actions = {str(event.get("action")) for event in governance.list_change_set_events(change_set_id)}
    assert {"approval_requested", "rejected"} <= actions


def test_repository_ops_route_per_agent_not_always_main(tmp_path):
    """缺陷②回归：repository_status/snapshot/current_ref 按 agent_id 路由到对应 per-agent 版本库，
    不再恒走 main 主库（per-agent 版本治理隔离）。"""
    governance, main_store = _governance(tmp_path)
    # main-agent 仍走传入的主库实例。
    assert governance._store_for("main-agent") is main_store
    # 业务 Agent 走独立 per-agent 库：不同实例、不同 repository_dir。
    biz_store = governance._store_for("biz-x")
    assert biz_store is not main_store
    assert main_store.repository_dir != biz_store.repository_dir
    assert "business-agents/biz-x/workspace" in str(biz_store.repository_dir)
    # repository_status 按 agent_id 路由：业务 Agent 的状态来自其自己的库，不是主库。
    biz_status = governance.repository_status("biz-x")
    main_status = governance.repository_status("main-agent")
    assert str(biz_store.repository_dir) == str(biz_status["repository_dir"])
    assert biz_status["repository_dir"] != main_status["repository_dir"]


def test_version_governance_rejects_unregistered_ghost_agent(tmp_path):
    """缺陷④：装配 agent_exists 后，未注册 agent_id 的版本治理操作被拒（404），不懒建幽灵版本库。"""
    governance, _ = _governance(tmp_path)
    governance.agent_exists = lambda aid: aid == "real-biz"
    with pytest.raises(AgentGovernanceError) as exc:
        governance.repository_status("ghost-agent")
    assert exc.value.status_code == 404
    # main-agent 恒有效；已注册的 real-biz 放行。
    assert governance.repository_status("main-agent")
    assert governance.repository_status("real-biz")
