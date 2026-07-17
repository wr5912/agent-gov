from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from inspect import signature
from pathlib import Path

import pytest
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.business_agent_workspace import seed_business_agent_workspace
from app.runtime.errors import ConflictError, DataIntegrityError, FeedbackStoreError
from app.runtime.improvement_db import AttributionModel, ExecutionRecordModel, ImprovementItemModel, OptimizationPlanModel
from app.runtime.records.eval_run_records import EvalRunProjectionRecord
from app.runtime.response_schemas.agent_governance_response_schemas import AgentChangeSetCreateRequest
from app.runtime.runtime_db import (
    AgentChangeSetModel,
    AgentReleaseModel,
    AgentReleaseSourceClaimModel,
    AgentReleaseTagClaimModel,
    EvalRunItemModel,
    EvalRunModel,
    TestDatasetCaseModel,
    utc_now,
)
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.test_dataset_schemas import TestCaseRecord as DatasetCaseRecord
from app.services.agent_change_set_provisioner import ChangeSetSource
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService
from sqlalchemy.exc import OperationalError

from feedback_store_test_utils import _seed_test_dataset, _settings


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
    governance = AgentGovernanceService(
        feedback_store=store,
        agent_version_store=agent_store,
        runtime_mode=settings.runtime_volume_mode,
        runtime_env={"MCP_SERVER_URL": "http://localhost:58001/mcp"},
    )
    # service 不再预置 main-agent 的版本 store（预置会在 main 被删除后留下悬空实例）。用例把
    # 返回的 agent_store 当作「main 的版本库」注入失败或断言状态，因此这里显式放进缓存，
    # 让它与 service 懒建的实例是同一个——否则 monkeypatch 打在一个 service 从不使用的对象上。
    governance._agent_stores["main-agent"] = agent_store
    return governance, agent_store


def _candidate_change_set(
    governance: AgentGovernanceService,
    agent_store: GitAgentVersionStore,
    *,
    content: str = "# Test Agent\n\n发布候选变更。\n",
    agent_id: str | None = None,
):
    if agent_id and agent_id != "main-agent":
        workspace = business_agent_layout(governance.feedback_store.data_dir, agent_id).workspace
        seed_business_agent_workspace(workspace, agent_id=agent_id, name=agent_id)
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


def _persist_regression_run(
    governance: AgentGovernanceService,
    change_set: dict,
    *,
    item_status: str = "passed",
    intent_id: str = "evr-intent-test",
    case_count: int = 1,
) -> dict:
    change_set_id = str(change_set["change_set_id"])
    candidate_commit = str(change_set["candidate_commit_sha"])
    source_improvement_id = f"imp-{change_set_id}"
    source_execution_id = str(change_set["execution_job_id"])
    dataset_id = _seed_test_dataset(
        governance.feedback_store,
        agent_id=str(change_set["agent_id"]),
        dataset_id=f"tds-{change_set_id}",
        candidate_agent_version_id=candidate_commit,
        source_improvement_id=source_improvement_id,
        source_execution_id=source_execution_id,
    )
    if case_count > 1:
        with governance.feedback_store.Session.begin() as db:
            db.add_all(
                TestDatasetCaseModel(
                    case_id=f"tdc-{dataset_id}-{position}",
                    dataset_id=dataset_id,
                    position=position,
                    prompt=f"验证 typed dataset 执行路径 {position}",
                    expected_behavior="返回非空且无运行错误的结果",
                    checkpoints_json=["输出非空"],
                )
                for position in range(2, case_count + 1)
            )
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        bound_at = utc_now()
        attribution_id = f"attr-{change_set_id}"
        optimization_plan_id = f"opt-{change_set_id}"
        db.add(
            ImprovementItemModel(
                improvement_id=source_improvement_id,
                agent_id=str(change_set["agent_id"]),
                title="候选回归来源",
                improvement_stage="regression",
                improvement_status="active",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            AttributionModel(
                attribution_id=attribution_id,
                improvement_id=source_improvement_id,
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            OptimizationPlanModel(
                optimization_plan_id=optimization_plan_id,
                improvement_id=source_improvement_id,
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            ExecutionRecordModel(
                execution_id=source_execution_id,
                improvement_id=source_improvement_id,
                change_set_id=change_set_id,
                status="confirmed",
                applied_agent_version_id=candidate_commit,
                source_optimization_plan_id=optimization_plan_id,
                source_optimization_plan_updated_at=bound_at,
                source_attribution_id=attribution_id,
                source_attribution_updated_at=bound_at,
            )
        )
        payload = dict(row.payload_json or {})
        payload["source_improvement_id"] = source_improvement_id
        payload["source_attribution_id"] = attribution_id
        row.payload_json = payload
    governance.mark_regression_running(
        change_set_id,
        eval_run_id=intent_id,
        dataset_id=dataset_id,
        operator="tester",
    )
    run = governance.feedback_store.create_eval_run(
        dataset_id=dataset_id,
        agent_version_id=candidate_commit,
        source="agent_change_set_regression",
        change_set_id=change_set_id,
        regression_attempt_id=intent_id,
        candidate_commit_sha=candidate_commit,
        candidate_worktree_path=str(change_set["worktree_path"]),
    )
    for dataset_case_payload in run["dataset_snapshot"]["cases"]:
        dataset_case = DatasetCaseRecord.model_validate(dataset_case_payload)
        governance.feedback_store.append_eval_run_item(
            str(run["eval_run_id"]),
            dataset_case=dataset_case,
            agent_result={
                "run_id": f"run-{change_set_id}-{dataset_case.case_id}",
                "agent_version_id": candidate_commit,
                "answer": "ok" if item_status == "passed" else "failed",
            },
            status=item_status,
            score=1.0 if item_status in {"passed", "needs_human_review"} else 0.0,
            check_results=([{"name": "runtime", "passed": True, "required": True, "detail": "运行证据完整"}] if item_status == "needs_human_review" else []),
        )
    finished = governance.feedback_store.finish_eval_run(str(run["eval_run_id"]))
    assert finished is not None
    return finished


def test_stable_change_set_intent_is_idempotent_and_candidate_is_immutable(tmp_path):
    governance, agent_store = _governance(tmp_path)
    stable_id = "agc-11111111-2222-3333-4444-555555555555"
    base = str(agent_store.current_commit_sha())
    first = governance.create_change_set(
        change_set_id=stable_id,
        base_commit_sha=base,
        execution_job_id="exec-stable",
        title="stable execution intent",
    )
    repeated = governance.create_change_set(
        change_set_id=stable_id,
        base_commit_sha=base,
        execution_job_id="exec-stable",
        title="stable execution intent",
    )
    assert repeated["change_set_id"] == first["change_set_id"]
    assert [event["action"] for event in governance.list_change_set_events(stable_id)] == ["created"]

    worktree = Path(str(first["worktree_path"]))
    worktree.joinpath("CLAUDE.md").write_text("first candidate\n", encoding="utf-8")
    first_candidate = agent_store.commit_worktree(worktree, message="first candidate")
    governance.mark_candidate_committed(stable_id, candidate_commit_sha=first_candidate, execution_job_id="exec-stable")
    worktree.joinpath("CLAUDE.md").write_text("stale second candidate\n", encoding="utf-8")
    stale_candidate = agent_store.commit_worktree(worktree, message="stale candidate")

    with pytest.raises(AgentGovernanceError, match="different candidate"):
        governance.mark_candidate_committed(stable_id, candidate_commit_sha=stale_candidate, execution_job_id="exec-stable")
    assert governance.get_change_set(stable_id)["candidate_commit_sha"] == first_candidate
    with pytest.raises(AgentGovernanceError, match="different execution"):
        governance.create_change_set(
            change_set_id=stable_id,
            base_commit_sha=base,
            execution_job_id="exec-other",
        )


def test_publish_cleans_candidate_worktree_and_retry_remains_idempotent(tmp_path):
    governance, agent_store = _governance(tmp_path)
    candidate = _candidate_change_set(governance, agent_store)
    change_set_id = str(candidate["change_set_id"])
    worktree = Path(str(candidate["worktree_path"]))
    branch = str(candidate["branch_name"])
    assert worktree.exists()

    release = governance.publish_change_set(change_set_id, operator="tester")
    repeated = governance.publish_change_set(change_set_id, operator="tester")

    assert repeated["release_id"] == release["release_id"]
    assert not worktree.exists()
    assert not agent_store._git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=agent_store.repository_dir, check=False).strip()


def test_abandon_cleans_worktree_but_keeps_unpublished_candidate_branch_for_audit(tmp_path):
    governance, agent_store = _governance(tmp_path)
    candidate = _candidate_change_set(governance, agent_store)
    change_set_id = str(candidate["change_set_id"])
    worktree = Path(str(candidate["worktree_path"]))
    branch_ref = f"refs/heads/{candidate['branch_name']}"

    abandoned = governance.abandon_change_set(change_set_id, operator="tester")
    repeated = governance.abandon_change_set(change_set_id, operator="tester")

    assert abandoned["status"] == "abandoned" and repeated["status"] == "abandoned"
    assert not worktree.exists()
    assert agent_store._git(["show-ref", "--verify", branch_ref], cwd=agent_store.repository_dir, check=False).strip()
    assert [event["action"] for event in governance.list_change_set_events(change_set_id)].count("abandoned") == 1
    with pytest.raises(AgentGovernanceError, match="cannot be published from status abandoned"):
        governance.publish_change_set(change_set_id)


def test_manual_change_set_has_no_fabricated_improvement_attribution(tmp_path):
    governance, _agent_store = _governance(tmp_path)

    change_set = governance.create_change_set(title="手工候选", operator="tester")

    assert change_set.get("source_improvement_id") is None
    assert change_set.get("source_attribution_id") is None
    assert change_set.get("source_attribution_status") is None
    with pytest.raises(ValueError, match="source_improvement_id"):
        AgentChangeSetCreateRequest.model_validate({"title": "伪造来源", "source_improvement_id": "imp-hostile", "source_attribution_status": "confirmed"})


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
    hook_path = worktree / "hooks" / "pre_tool_guard.py"
    hook_path.unlink()
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


def test_business_agent_version_chain_is_isolated_from_main(tmp_path):
    """B3.2/B3.3（AGV-017 per-agent 版本隔离）：业务 Agent 的 change set/release 落在自己独立的版本 store，与 main 互不混淆。"""
    governance, main_store = _governance(tmp_path)
    main_head_before = main_store.current_commit_sha()

    # 为业务 Agent 创建 → 提交 → 发布一条独立版本记录。
    biz_change_set = _candidate_change_set(governance, main_store, content="# Biz Agent\n\n业务 Agent 候选。\n", agent_id="biz-agent-001")
    assert biz_change_set["agent_id"] == "biz-agent-001"
    biz_release = governance.publish_change_set(str(biz_change_set["change_set_id"]), operator="tester")
    assert biz_release["agent_id"] == "biz-agent-001"

    # 隔离性：发布业务 Agent 版本不改动 main 版本链（main HEAD 不变）。
    assert main_store.current_commit_sha() == main_head_before
    biz_store = governance._store_for("biz-agent-001")
    assert biz_store.repository_dir != main_store.repository_dir
    assert biz_store.current_commit_sha() == biz_release["commit_sha"]
    assert biz_store.repository_dir != main_store.repository_dir

    # 按 Agent 过滤互不串扰：各自只看到自己的 change set/release。
    assert [cs["change_set_id"] for cs in governance.list_change_sets(agent_id="biz-agent-001")] == [biz_change_set["change_set_id"]]
    assert governance.list_change_sets(agent_id="main-agent") == []
    assert [rel["release_id"] for rel in governance.list_releases(agent_id="biz-agent-001")] == [biz_release["release_id"]]
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
        # 每个业务 Agent 一条独立闭环记录：run→signal→case + change set/release(版本) + eval(评估)。
        store.record_run({"run_id": f"run-{agent_id}", "agent_id": agent_id, "created_at": "2026-06-12T00:00:00Z"})
        signal = store.create_signal(FeedbackSignalCreateRequest(run_id=f"run-{agent_id}", labels=["tool_data_incomplete"]))
        case = store.create_case(source_refs=[("signal", signal["signal_id"])], title=f"{agent_id} 反馈")
        change_set = _candidate_change_set(governance, main_store, content=f"# {agent_id}\n\n候选\n", agent_id=agent_id)
        release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
        dataset_id = _seed_test_dataset(store, agent_id=agent_id, dataset_id=f"tds-{agent_id}")
        eval_run = store.create_eval_run(
            dataset_id=dataset_id,
            agent_version_id=release["commit_sha"],
            change_set_id=str(change_set["change_set_id"]),
        )
        records[agent_id] = {"signal": signal, "case": case, "change_set": change_set, "release": release, "eval": eval_run}

    # 治理 Agent（单一 governance 实例）为不同业务 Agent 各自管理独立版本 store（物理隔离）。
    assert governance._store_for("agent-alpha") is not governance._store_for("agent-beta")

    # 每个维度按 Agent 过滤只见自身记录，不被另一个 Agent 串扰。
    for agent_id in agents:
        assert {r["agent_id"] for r in store.list_runs(agent_id=agent_id)} == {agent_id}
        assert {s["agent_id"] for s in store.list_signals(agent_id=agent_id)} == {agent_id}
        assert records[agent_id]["case"]["agent_id"] == agent_id
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


def test_publish_retries_after_archive_failure_without_duplicate_release(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    real_archive_ref = agent_store.archive_ref

    def fail_archive(_ref: str):
        raise AgentGitError("injected archive failure")

    monkeypatch.setattr(agent_store, "archive_ref", fail_archive)
    with pytest.raises(AgentGovernanceError, match="injected archive failure"):
        governance.publish_change_set(change_set_id, operator="tester")

    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert pending["publication_error"]["detail"] == "injected archive failure"
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert governance.list_releases() == []
    intent = pending["publication_intent"]
    assert (
        agent_store._git(
            ["rev-parse", "--verify", f"refs/tags/{intent['tag_name']}^{{commit}}"],
            cwd=agent_store.repository_dir,
        ).strip()
        == change_set["candidate_commit_sha"]
    )

    monkeypatch.setattr(agent_store, "archive_ref", real_archive_ref)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")

    assert release["release_id"] == intent["release_id"]
    assert Path(str(release["archive_path"])).is_file()
    assert len(governance.list_releases()) == 1
    assert governance.get_change_set(change_set_id)["status"] == "published"


def test_publish_db_finalize_failure_rolls_back_metadata_and_retry_reconciles(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    real_add_event = governance._add_event_row

    def fail_published_event(db, target_change_set_id, action, operator, *, before, after):
        if action == "published":
            raise OperationalError("INSERT agent_change_set_events", {}, RuntimeError("injected DB failure"))
        return real_add_event(
            db,
            target_change_set_id,
            action,
            operator,
            before=before,
            after=after,
        )

    monkeypatch.setattr(governance, "_add_event_row", fail_published_event)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(change_set_id, operator="tester")

    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert governance.list_releases() == []
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]

    monkeypatch.setattr(governance, "_add_event_row", real_add_event)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")
    events = governance.list_change_set_events(change_set_id)

    assert release["release_id"] == pending["publication_intent"]["release_id"]
    assert len(governance.list_releases()) == 1
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_publish_finishes_metadata_without_overwriting_newer_source_after_git_side_effect(tmp_path, monkeypatch):
    import app.services.agent_publication_finalization as finalization_module

    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)

    def source_changed(*_args, **_kwargs):
        raise ConflictError("Source improvement changed during publication finalization")

    monkeypatch.setattr(finalization_module, "finalize_intent_source", source_changed)
    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    projected = governance.get_change_set(str(change_set["change_set_id"]))
    assert release["status"] == "published"
    assert projected["status"] == "published"
    assert release["source_finalization_conflict"]["detail"] == ("Source improvement changed during publication finalization")
    assert projected["source_finalization_conflict"] == release["source_finalization_conflict"]
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]


def test_source_claim_blocks_second_publication_before_git_side_effect(tmp_path, monkeypatch):
    import app.services.agent_publication_finalization as finalization_module

    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# first source publication\n")
    first_run = _persist_regression_run(governance, first, intent_id="evr-source-claim-first")
    governance.complete_regression(
        str(first["change_set_id"]),
        eval_run_id=str(first_run["eval_run_id"]),
        operator="tester",
    )

    def source_changed(*_args, **_kwargs):
        raise ConflictError("Source improvement changed during publication finalization")

    monkeypatch.setattr(finalization_module, "finalize_intent_source", source_changed)
    first_release = governance.publish_change_set(str(first["change_set_id"]), operator="tester")
    published_head = agent_store.current_commit_sha()
    source_improvement_id = str(first_release["source_improvement_id"])

    second = _candidate_change_set(governance, agent_store, content="# second source publication\n")
    with governance.feedback_store.Session.begin() as db:
        first_row = db.get(AgentChangeSetModel, str(first["change_set_id"]))
        second_row = db.get(AgentChangeSetModel, str(second["change_set_id"]))
        execution = db.query(ExecutionRecordModel).filter_by(improvement_id=source_improvement_id).one()
        assert first_row is not None and second_row is not None
        second_payload = dict(second_row.payload_json or {})
        second_payload.update(
            {
                "source_improvement_id": source_improvement_id,
                "source_attribution_id": (first_row.payload_json or {})["source_attribution_id"],
            }
        )
        second_row.payload_json = second_payload
        execution.change_set_id = str(second["change_set_id"])
        execution.applied_agent_version_id = str(second["candidate_commit_sha"])

    with pytest.raises(AgentGovernanceError, match="持有发布预留，不能重复发布"):
        governance.publish_change_set(str(second["change_set_id"]), operator="tester")

    assert agent_store.current_commit_sha() == published_head
    assert governance.get_change_set(str(second["change_set_id"]))["status"] == "candidate_committed"
    with governance.feedback_store.Session() as db:
        claim = db.get(AgentReleaseSourceClaimModel, ("main-agent", source_improvement_id))
        assert claim is not None and claim.change_set_id == first["change_set_id"]


def test_improvement_publication_rejects_unconfirmed_or_revised_provenance_even_with_force(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set_id = "agc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    bound_at = "2026-07-10T00:00:00+00:00"
    with governance.feedback_store.Session.begin() as db:
        db.add(
            ImprovementItemModel(
                improvement_id="imp-publish",
                agent_id="main-agent",
                title="来源治理",
                improvement_stage="regression",
                improvement_status="active",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            AttributionModel(
                attribution_id="attr-publish",
                improvement_id="imp-publish",
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            OptimizationPlanModel(
                optimization_plan_id="opt-publish",
                improvement_id="imp-publish",
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            ExecutionRecordModel(
                execution_id="exec-publish",
                improvement_id="imp-publish",
                change_set_id=change_set_id,
                status="confirmed",
                source_optimization_plan_id="opt-publish",
                source_optimization_plan_updated_at=bound_at,
                source_attribution_id="attr-publish",
                source_attribution_updated_at=bound_at,
            )
        )
    change_set = governance.create_change_set(
        change_set_id=change_set_id,
        execution_job_id="exec-publish",
        source=ChangeSetSource("imp-publish", "attr-publish", "confirmed"),
    )
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("CLAUDE.md").write_text("provenance candidate\n", encoding="utf-8")
    candidate = agent_store.commit_worktree(worktree, message="provenance candidate")
    governance.mark_candidate_committed(change_set_id, candidate_commit_sha=candidate, execution_job_id="exec-publish")

    with governance.feedback_store.Session.begin() as db:
        db.get(ExecutionRecordModel, "exec-publish").status = "draft"

    projected = governance.get_change_set(change_set_id)
    assert projected["publication_provenance_blocker"] == "改进执行尚未确认或执行来源不完整，请先确认执行结果"
    assert projected["publication_blocker"] == projected["publication_provenance_blocker"]
    with pytest.raises(ConflictError, match="执行尚未确认"):
        governance.publish_change_set(change_set_id, operator="tester")

    with governance.feedback_store.Session.begin() as db:
        db.get(ExecutionRecordModel, "exec-publish").status = "confirmed"
        attribution = db.get(AttributionModel, "attr-publish")
        attribution.status = "draft"
        attribution.updated_at = "2026-07-10T00:01:00+00:00"

    assert governance.get_change_set(change_set_id)["source_attribution_status"] == "draft"
    for force in (False, True):
        with pytest.raises(ConflictError, match="归因未确认"):
            governance.publish_change_set(change_set_id, operator="tester", force=force)
    assert governance.get_change_set(change_set_id)["status"] == "candidate_committed"
    assert agent_store.current_commit_sha() != candidate

    with governance.feedback_store.Session.begin() as db:
        attribution = db.get(AttributionModel, "attr-publish")
        execution = db.get(ExecutionRecordModel, "exec-publish")
        plan = db.get(OptimizationPlanModel, "opt-publish")
        attribution.status = "confirmed"
        execution.source_attribution_updated_at = attribution.updated_at
        plan.status = "draft"
        plan.updated_at = "2026-07-10T00:02:00+00:00"

    with pytest.raises(ConflictError, match="优化方案未确认"):
        governance.publish_change_set(change_set_id, operator="tester", force=True)

    with governance.feedback_store.Session.begin() as db:
        execution = db.get(ExecutionRecordModel, "exec-publish")
        plan = db.get(OptimizationPlanModel, "opt-publish")
        plan.status = "confirmed"
        execution.source_optimization_plan_updated_at = plan.updated_at

    real_add_event = governance._add_event_row

    def fail_published_event(db, target_change_set_id, action, operator, *, before, after):
        if action == "published":
            raise OperationalError("INSERT agent_change_set_events", {}, RuntimeError("injected source finalize failure"))
        return real_add_event(db, target_change_set_id, action, operator, before=before, after=after)

    monkeypatch.setattr(governance, "_add_event_row", fail_published_event)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(change_set_id, operator="tester")

    with governance.feedback_store.Session() as db:
        rolled_back_item = db.get(ImprovementItemModel, "imp-publish")
        assert (rolled_back_item.improvement_stage, rolled_back_item.improvement_status, rolled_back_item.updated_at) == (
            "regression",
            "active",
            bound_at,
        )
    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert pending["publication_intent"]["source_improvement_updated_at"] == bound_at

    monkeypatch.setattr(governance, "_add_event_row", real_add_event)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")
    with governance.feedback_store.Session() as db:
        completed_item = db.get(ImprovementItemModel, "imp-publish")

    assert release["source_improvement_id"] == "imp-publish"
    assert completed_item.improvement_stage == "release"
    assert completed_item.improvement_status == "done"
    assert completed_item.updated_at == release["updated_at"]


def test_publish_retry_finalizes_older_tag_after_newer_release_advances_head(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv1\n")
    first_id = str(first["change_set_id"])
    real_add_event = governance._add_event_row

    def fail_first_finalize(db, change_set_id, action, operator, *, before, after):
        if change_set_id == first_id and action == "published":
            raise OperationalError("INSERT agent_change_set_events", {}, RuntimeError("injected DB failure"))
        return real_add_event(db, change_set_id, action, operator, before=before, after=after)

    monkeypatch.setattr(governance, "_add_event_row", fail_first_finalize)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(first_id, operator="tester")
    monkeypatch.setattr(governance, "_add_event_row", real_add_event)

    second = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv2\n")
    second_release = governance.publish_change_set(str(second["change_set_id"]), operator="tester")
    first_release = governance.publish_change_set(first_id, operator="reconciler")

    assert first_release["commit_sha"] == first["candidate_commit_sha"]
    assert agent_store.current_commit_sha() == second_release["commit_sha"]
    assert governance.get_change_set(first_id)["status"] == "published"
    assert len(governance.list_releases()) == 2


def test_divergent_candidate_publish_failure_cancels_intent_and_tag_claim(tmp_path):
    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nbranch-a\n")
    second = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nbranch-b\n")
    governance.publish_change_set(str(first["change_set_id"]), tag_name="release-branch-a")

    with pytest.raises(AgentGovernanceError, match="intent was cancelled before side effects"):
        governance.publish_change_set(str(second["change_set_id"]), tag_name="release-branch-b")

    persisted = governance.get_change_set(str(second["change_set_id"]))
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted
    assert persisted["publication_error"]["detail"]
    actions = [event["action"] for event in governance.list_change_set_events(str(second["change_set_id"]))]
    assert actions.count("publication_started") == 1
    assert actions.count("publication_cancelled") == 1
    with governance.feedback_store.Session() as db:
        assert db.get(AgentReleaseTagClaimModel, ("main-agent", "release-branch-b")) is None


def test_repeated_publish_returns_same_release_and_rejects_conflicting_tag(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    first = governance.publish_change_set(change_set_id, operator="tester")
    repeated = governance.publish_change_set(
        change_set_id,
        operator="retrying-operator",
        tag_name=str(first["tag_name"]),
    )

    assert repeated["release_id"] == first["release_id"]
    assert len(governance.list_releases()) == 1
    actions = [event["action"] for event in governance.list_change_set_events(change_set_id)]
    assert actions.count("publication_started") == 1
    assert actions.count("published") == 1
    with pytest.raises(AgentGovernanceError, match="already published with a different tag"):
        governance.publish_change_set(change_set_id, tag_name="agent-release-conflict")


def test_release_tag_is_owned_by_one_change_set_per_agent(tmp_path):
    governance, agent_store = _governance(tmp_path)
    shared_tag = "agent-release-shared-candidate"
    first = _candidate_change_set(governance, agent_store)
    first_release = governance.publish_change_set(
        str(first["change_set_id"]),
        operator="tester",
        tag_name=shared_tag,
    )
    second = governance.create_change_set(
        base_commit_sha=str(first["candidate_commit_sha"]),
        title="same candidate, different change set",
        operator="tester",
    )
    second = governance.mark_candidate_committed(
        str(second["change_set_id"]),
        candidate_commit_sha=str(first["candidate_commit_sha"]),
        execution_job_id="job-same-candidate",
        operator="tester",
    )

    with pytest.raises(AgentGovernanceError, match="already assigned to another release"):
        governance.publish_change_set(str(second["change_set_id"]), tag_name=shared_tag)

    persisted = governance.get_change_set(str(second["change_set_id"]))
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted
    assert "publication_started" not in {event["action"] for event in governance.list_change_set_events(str(second["change_set_id"]))}
    business = _candidate_change_set(
        governance,
        agent_store,
        content="# Business Agent\n\nsame tag, isolated repository\n",
        agent_id="biz-shared-tag",
    )
    business_release = governance.publish_change_set(
        str(business["change_set_id"]),
        tag_name=shared_tag,
    )
    assert first_release["tag_name"] == business_release["tag_name"] == shared_tag
    assert first_release["agent_id"] != business_release["agent_id"]


def test_concurrent_publish_reserves_one_intent_and_one_audit_event(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    publish_entered = threading.Event()
    allow_publish = threading.Event()
    real_publish_commit = agent_store.publish_commit

    def synchronized_publish(commit_sha: str, *, tag_name: str, message: str, validate_ref=None):
        publish_entered.set()
        assert allow_publish.wait(timeout=10)
        return real_publish_commit(commit_sha, tag_name=tag_name, message=message, validate_ref=validate_ref)

    monkeypatch.setattr(agent_store, "publish_commit", synchronized_publish)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(governance.publish_change_set, change_set_id, operator="publisher-0")
        assert publish_entered.wait(timeout=10)
        second = executor.submit(governance.publish_change_set, change_set_id, operator="publisher-1")
        with pytest.raises(AgentGovernanceError, match="maintenance"):
            second.result(timeout=10)
        allow_publish.set()
        release = first.result(timeout=30)

    assert release["release_id"]
    assert len(governance.list_releases()) == 1
    events = governance.list_change_set_events(change_set_id)
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_concurrent_publish_with_different_tags_is_fenced_before_db_reservation(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    publish_entered = threading.Event()
    allow_publish = threading.Event()
    real_publish_commit = agent_store.publish_commit

    def synchronized_publish(commit_sha: str, *, tag_name: str, message: str, validate_ref=None):
        publish_entered.set()
        assert allow_publish.wait(timeout=10)
        return real_publish_commit(commit_sha, tag_name=tag_name, message=message, validate_ref=validate_ref)

    monkeypatch.setattr(agent_store, "publish_commit", synchronized_publish)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            governance.publish_change_set,
            change_set_id,
            operator="publisher-0",
            tag_name="agent-release-competing-0",
        )
        assert publish_entered.wait(timeout=10)
        second = executor.submit(
            governance.publish_change_set,
            change_set_id,
            operator="publisher-1",
            tag_name="agent-release-competing-1",
        )
        with pytest.raises(AgentGovernanceError, match="maintenance") as exc:
            second.result(timeout=10)
        allow_publish.set()
        release = first.result(timeout=30)

    assert exc.value.status_code == 409
    assert release["tag_name"] == "agent-release-competing-0"
    assert len(governance.list_releases()) == 1
    events = governance.list_change_set_events(change_set_id)
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_invalid_explicit_tag_is_rejected_before_intent_is_reserved(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    with pytest.raises(AgentGovernanceError, match="Invalid release tag name"):
        governance.publish_change_set(change_set_id, tag_name="--hostile-option")

    persisted = governance.get_change_set(change_set_id)
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted


def test_publish_reconciles_legacy_release_row_without_duplicate(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    tag_name = "agent-release-legacy-partial"
    git_result = agent_store.publish_commit(
        str(change_set["candidate_commit_sha"]),
        tag_name=tag_name,
        message="legacy partial publication",
    )
    archive = git_result["archive"]
    now = utc_now()
    legacy_release_id = "agr-legacy-partial"
    legacy_payload = {
        "schema_version": "agent-release/v1",
        "release_id": legacy_release_id,
        "agent_id": "main-agent",
        "created_at": now,
        "updated_at": now,
        "status": "published",
        "tag_name": tag_name,
        "commit_sha": change_set["candidate_commit_sha"],
        "change_set_id": change_set_id,
        "archive_path": archive["archive_path"],
        "archive_sha256": archive["sha256"],
    }
    with governance.feedback_store.Session.begin() as db:
        db.add(
            AgentReleaseModel(
                release_id=legacy_release_id,
                agent_id="main-agent",
                created_at=now,
                updated_at=now,
                status="published",
                tag_name=tag_name,
                commit_sha=str(change_set["candidate_commit_sha"]),
                change_set_id=change_set_id,
                archive_path=str(archive["archive_path"]),
                payload_json=legacy_payload,
            )
        )

    release = governance.publish_change_set(change_set_id, operator="reconciler")

    assert release["release_id"] == legacy_release_id
    assert len(governance.list_releases()) == 1
    assert governance.get_change_set(change_set_id)["latest_release_id"] == legacy_release_id


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


def test_change_set_regression_failed_cases_block_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    eval_run = _persist_regression_run(governance, change_set, item_status="failed")
    regression_failed = governance.complete_regression(
        str(change_set["change_set_id"]),
        eval_run_id=str(eval_run["eval_run_id"]),
        operator="tester",
    )

    assert regression_failed["status"] == "regression_failed"
    assert "回归验证存在失败用例" in regression_failed["publication_blocker"]
    with pytest.raises(AgentGovernanceError, match="回归验证存在失败用例") as exc:
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert exc.value.status_code == 409
    assert agent_store.current_commit_sha() != change_set["candidate_commit_sha"]


def test_regression_human_review_approval_is_audited_idempotent_and_publishable(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set, item_status="needs_human_review")
    awaiting_review = governance.complete_regression(
        change_set_id,
        eval_run_id=str(eval_run["eval_run_id"]),
        operator="runner",
    )
    decision = {
        "review_id": "review-approved",
        "operator": "reviewer",
        "reason": "已核验候选输出满足自然语言期望",
        "scope": "current_eval_run",
        "items": [{"dataset_case_id": eval_run["items"][0]["dataset_case_id"], "decision": "approve", "note": "证据一致"}],
    }

    assert awaiting_review["status"] == "regression_review_required"
    assert awaiting_review["publication_blocker"]
    review_plan = governance._plan_regression_review(eval_run["eval_run_id"], **decision)
    assert isinstance(review_plan.original_record, EvalRunProjectionRecord)
    assert isinstance(review_plan.reviewed_record, EvalRunProjectionRecord)
    assert review_plan.original_record.gate_result.status == "review_required"
    assert review_plan.reviewed_record.gate_result.status == "passed_with_notes"
    reviewed = governance.review_regression(change_set_id, eval_run_id=eval_run["eval_run_id"], **decision)
    repeated = governance.review_regression(change_set_id, eval_run_id=eval_run["eval_run_id"], **decision)
    current = governance.get_change_set(change_set_id)

    assert reviewed == repeated
    assert reviewed["result_status"] == "passed_with_notes"
    assert reviewed["items"][0]["status"] == "needs_human_review"
    assert reviewed["gate_result"]["review_dataset_case_ids"] == []
    assert reviewed["gate_result"]["review_decision"]["review_id"] == "review-approved"
    assert current is not None and current["status"] == "regression_passed"
    assert current["publication_blocker"] is None
    review_events = [event for event in governance.list_change_set_events(change_set_id) if event["action"] == "regression_review_approved"]
    assert len(review_events) == 1
    assert review_events[0]["operator"] == "reviewer"
    assert review_events[0]["after"]["latest_eval_run"]["gate_result"]["review_decision"]["reason"] == decision["reason"]
    with pytest.raises(ConflictError, match="different review decision"):
        governance.review_regression(
            change_set_id,
            eval_run_id=eval_run["eval_run_id"],
            **{**decision, "items": [{**decision["items"][0], "decision": "reject"}]},
        )

    release = governance.publish_change_set(change_set_id, operator="publisher", force=False)
    assert release["commit_sha"] == change_set["candidate_commit_sha"]


def test_regression_human_review_rejection_stays_blocked(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set, item_status="needs_human_review")
    governance.complete_regression(change_set_id, eval_run_id=eval_run["eval_run_id"], operator="runner")

    reviewed = governance.review_regression(
        change_set_id,
        eval_run_id=eval_run["eval_run_id"],
        review_id="review-rejected",
        operator="reviewer",
        reason="候选输出不满足关键检查点",
        scope="current_eval_run",
        items=[{"dataset_case_id": eval_run["items"][0]["dataset_case_id"], "decision": "reject", "note": "缺少核验"}],
    )
    current = governance.get_change_set(change_set_id)

    assert reviewed["result_status"] == "failed"
    assert reviewed["items"][0]["status"] == "needs_human_review"
    assert reviewed["gate_result"]["status"] == "blocked"
    assert current is not None and current["status"] == "regression_failed"
    assert current["publication_blocker"]
    with pytest.raises(AgentGovernanceError, match="回归验证存在失败用例"):
        governance.publish_change_set(change_set_id, operator="publisher", force=False)


def test_mixed_human_review_force_publish_records_only_rejected_cases(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(
        governance,
        change_set,
        item_status="needs_human_review",
        case_count=2,
    )
    governance.complete_regression(change_set_id, eval_run_id=eval_run["eval_run_id"], operator="runner")
    case_ids = [str(item["dataset_case_id"]) for item in eval_run["items"]]

    governance.review_regression(
        change_set_id,
        eval_run_id=eval_run["eval_run_id"],
        review_id="review-mixed",
        operator="reviewer",
        reason="一条满足预期，一条缺少关键核验",
        scope="current_eval_run",
        items=[
            {"dataset_case_id": case_ids[0], "decision": "approve", "note": "证据一致"},
            {"dataset_case_id": case_ids[1], "decision": "reject", "note": "缺少核验"},
        ],
    )
    blocked = governance.get_change_set(change_set_id)
    assert blocked is not None
    assert "1 条用例经人工复核拒绝" in blocked["publication_blocker"]
    assert "2 条用例" not in blocked["publication_blocker"]

    governance.publish_change_set(
        change_set_id,
        operator="lead",
        note="已审计人工拒绝项并接受发布风险",
        force=True,
    )
    published = governance.get_change_set(change_set_id)
    assert published is not None
    assert "1 条用例经人工复核拒绝" in published["force_publication_blocker"]
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]


def test_regression_human_review_requires_exact_current_case_scope(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set, item_status="needs_human_review")
    governance.complete_regression(change_set_id, eval_run_id=eval_run["eval_run_id"], operator="runner")

    with pytest.raises(ConflictError, match="cover exactly"):
        governance.review_regression(
            change_set_id,
            eval_run_id=eval_run["eval_run_id"],
            review_id="review-wrong-case",
            operator="reviewer",
            reason="错误 case 绑定",
            scope="current_eval_run",
            items=[{"dataset_case_id": "tdc-other", "decision": "approve"}],
        )

    with governance.feedback_store.Session.begin() as db:
        item = db.get(EvalRunItemModel, eval_run["items"][0]["eval_run_item_id"])
        assert item is not None
        payload = dict(item.payload_json or {})
        payload["check_results"] = [{"name": "runtime", "passed": False, "required": True, "detail": "失败"}]
        item.payload_json = payload
    with pytest.raises(ConflictError, match="failed required checks"):
        governance.review_regression(
            change_set_id,
            eval_run_id=eval_run["eval_run_id"],
            review_id="review-required-failed",
            operator="reviewer",
            reason="不得覆盖 required check",
            scope="current_eval_run",
            items=[{"dataset_case_id": eval_run["items"][0]["dataset_case_id"], "decision": "approve"}],
        )


def test_concurrent_regression_reviews_have_one_audited_winner(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set, item_status="needs_human_review")
    governance.complete_regression(change_set_id, eval_run_id=eval_run["eval_run_id"], operator="runner")
    case_id = eval_run["items"][0]["dataset_case_id"]
    barrier = threading.Barrier(2)

    def review(review_id: str, decision: str):
        barrier.wait()
        return governance.review_regression(
            change_set_id,
            eval_run_id=eval_run["eval_run_id"],
            review_id=review_id,
            operator=review_id,
            reason=f"并发决策 {decision}",
            scope="current_eval_run",
            items=[{"dataset_case_id": case_id, "decision": decision}],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(review, "review-approve", "approve"), executor.submit(review, "review-reject", "reject")]
        outcomes = [future.exception() or future.result() for future in futures]

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, FeedbackStoreError)]
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    persisted = governance.feedback_store.get_eval_run(eval_run["eval_run_id"])
    assert persisted is not None
    review_id = persisted["gate_result"]["review_decision"]["review_id"]
    assert review_id in {"review-approve", "review-reject"}
    review_events = [
        event for event in governance.list_change_set_events(change_set_id) if event["action"] in {"regression_review_approved", "regression_review_rejected"}
    ]
    assert len(review_events) == 1


def test_startup_regression_reconciliation_has_no_fixed_500_row_cap(tmp_path, monkeypatch):
    governance, _agent_store = _governance(tmp_path)
    now = utc_now()
    rows = [
        AgentChangeSetModel(
            change_set_id=f"agc-reconcile-{index:04d}",
            agent_id="main-agent",
            created_at=now,
            updated_at=now,
            status="regression_running",
            base_commit_sha="base",
            candidate_commit_sha="candidate",
            branch_name=f"agent-change/reconcile-{index:04d}",
            worktree_path=f"/tmp/reconcile-{index:04d}",
            payload_json={"latest_eval_run_id": f"intent-{index:04d}", "regression_attempt_id": f"intent-{index:04d}"},
        )
        for index in range(501)
    ]
    with governance.feedback_store.Session.begin() as db:
        db.add_all(rows)
    processed: list[str] = []
    monkeypatch.setattr(
        governance,
        "get_change_set",
        lambda change_set_id: {
            "change_set_id": change_set_id,
            "status": "regression_running",
            "latest_eval_run_id": change_set_id.replace("agc-reconcile", "intent"),
            "regression_attempt_id": change_set_id.replace("agc-reconcile", "intent"),
        },
    )
    monkeypatch.setattr(governance, "_get_persisted_regression_eval_run_by_attempt", lambda _attempt_id: None)
    monkeypatch.setattr(
        governance,
        "fail_regression",
        lambda change_set_id, **_kwargs: processed.append(change_set_id),
    )

    summary = governance.reconcile_regression_runs()

    assert len(processed) == 501
    assert len(summary["failed"]) == 501
    assert summary["errors"] == []


@pytest.mark.parametrize("eval_run", [None, {"status": "running", "eval_run_id": "evr-active"}])
def test_regression_reconciliation_does_not_preempt_fresh_or_running_attempt(
    tmp_path,
    monkeypatch,
    eval_run,
):
    governance, _agent_store = _governance(tmp_path)
    now = utc_now()
    with governance.feedback_store.Session.begin() as db:
        db.add(
            AgentChangeSetModel(
                change_set_id="agc-active-recovery",
                agent_id="main-agent",
                created_at=now,
                updated_at=now,
                status="regression_running",
                base_commit_sha="base",
                candidate_commit_sha="candidate",
                branch_name="agent-change/active-recovery",
                worktree_path="/tmp/active-recovery",
                payload_json={
                    "latest_eval_run_id": "intent-active",
                    "regression_attempt_id": "intent-active",
                    "regression_started_at": now,
                },
            )
        )
    monkeypatch.setattr(
        governance,
        "_get_persisted_regression_eval_run_by_attempt",
        lambda _attempt_id: eval_run,
    )
    failed: list[str] = []
    monkeypatch.setattr(governance, "fail_regression", lambda change_set_id, **_kwargs: failed.append(change_set_id))

    assert governance.reconcile_regression_runs(now=now) == {"completed": [], "failed": [], "errors": []}
    assert failed == []


def test_regression_completion_service_contract_only_accepts_persisted_eval_run_id():
    parameters = set(signature(AgentGovernanceService.complete_regression).parameters)

    assert parameters == {"self", "change_set_id", "eval_run_id", "operator"}


def test_bound_persisted_regression_can_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set, intent_id="evr-intent-bound")

    completed = governance.complete_regression(
        change_set_id,
        eval_run_id=str(eval_run["eval_run_id"]),
        operator="tester",
    )
    release = governance.publish_change_set(change_set_id, operator="tester")

    assert completed["status"] == "regression_passed"
    assert completed["latest_eval_run_id"] == eval_run["eval_run_id"]
    assert release["commit_sha"] == change_set["candidate_commit_sha"]


def test_startup_reconciles_terminal_and_missing_regression_attempts(tmp_path):
    governance, agent_store = _governance(tmp_path)
    terminal_change_set = _candidate_change_set(governance, agent_store, content="# terminal\n")
    terminal_run = _persist_regression_run(governance, terminal_change_set, intent_id="evr-intent-terminal")
    missing_change_set = _candidate_change_set(governance, agent_store, content="# missing\n")
    governance.mark_regression_running(
        str(missing_change_set["change_set_id"]),
        eval_run_id="evr-intent-missing-after-restart",
        dataset_id="tds-missing-after-restart",
        operator="tester",
    )

    summary = governance.reconcile_regression_runs(now="2999-01-01T00:00:00+00:00")

    assert summary["completed"] == [terminal_change_set["change_set_id"]]
    assert summary["failed"] == [missing_change_set["change_set_id"]]
    assert summary["errors"] == []
    terminal = governance.get_change_set(str(terminal_change_set["change_set_id"]))
    missing = governance.get_change_set(str(missing_change_set["change_set_id"]))
    assert terminal is not None and terminal["status"] == "regression_passed"
    assert terminal["latest_eval_run_id"] == terminal_run["eval_run_id"]
    assert missing is not None and missing["status"] == "regression_failed"
    assert missing["regression_error"]["error_type"] == "LeaseExpired"
    assert governance.reconcile_regression_runs() == {"completed": [], "failed": [], "errors": []}


@pytest.mark.parametrize(
    "binding",
    [
        "change_set_id",
        "regression_attempt_id",
        "agent_id",
        "candidate_commit_sha",
        "agent_version_id",
        "candidate_worktree_path",
        "dataset_id",
        "source",
        "complete_items",
        "dataset_candidate_agent_version_id",
        "dataset_source_improvement_id",
        "dataset_source_execution_id",
        "running_status",
        "duplicate_snapshot_case",
        "duplicate_item",
        "item_case_snapshot",
        "item_eval_run_id",
        "naive_created_at",
        "completed_before_created",
    ],
)
def test_complete_regression_rejects_unbound_persisted_eval_run(tmp_path, binding, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set)
    eval_run_id = str(eval_run["eval_run_id"])
    projected_mutations = {
        "dataset_candidate_agent_version_id",
        "dataset_source_improvement_id",
        "dataset_source_execution_id",
        "running_status",
        "duplicate_snapshot_case",
        "duplicate_item",
        "item_case_snapshot",
        "item_eval_run_id",
        "naive_created_at",
        "completed_before_created",
    }
    if binding in projected_mutations:
        hostile = deepcopy(eval_run)
        if binding == "dataset_candidate_agent_version_id":
            hostile["dataset_snapshot"]["provenance"]["candidate_agent_version_id"] = "candidate-other"
        elif binding == "dataset_source_improvement_id":
            hostile["dataset_snapshot"]["source_improvement_id"] = "imp-other"
        elif binding == "dataset_source_execution_id":
            hostile["dataset_snapshot"]["provenance"]["execution_id"] = "exec-other"
        elif binding == "running_status":
            hostile.update({"status": "running", "result_status": "running", "completed_at": None})
        elif binding == "duplicate_snapshot_case":
            hostile["dataset_snapshot"]["cases"].append(deepcopy(hostile["dataset_snapshot"]["cases"][0]))
        elif binding == "duplicate_item":
            duplicate = deepcopy(hostile["items"][0])
            duplicate["eval_run_item_id"] = "evi-hostile-duplicate"
            hostile["items"].append(duplicate)
        elif binding == "item_case_snapshot":
            hostile["items"][0]["dataset_case_snapshot"]["prompt"] = "hostile changed prompt"
        elif binding == "item_eval_run_id":
            hostile["items"][0]["eval_run_id"] = "evr-other"
        elif binding == "naive_created_at":
            hostile["created_at"] = "2026-07-13T12:00:00"
        elif binding == "completed_before_created":
            hostile["completed_at"] = "2000-01-01T00:00:00+00:00"
        monkeypatch.setattr(governance, "_get_persisted_regression_eval_run", lambda _eval_run_id: hostile)
    else:
        with governance.feedback_store.Session.begin() as db:
            row = db.get(EvalRunModel, eval_run_id)
            assert row is not None
            payload = dict(row.payload_json or {})
            if binding == "change_set_id":
                payload["change_set_id"] = "agc-other"
            elif binding == "regression_attempt_id":
                payload["regression_attempt_id"] = "evr-intent-other"
            elif binding == "agent_id":
                row.agent_id = "other-agent"
                snapshot = dict(payload["dataset_snapshot"])
                snapshot["agent_id"] = "other-agent"
                snapshot["owner_id"] = "other-agent"
                payload["dataset_snapshot"] = snapshot
            elif binding == "candidate_commit_sha":
                payload["candidate_commit_sha"] = "candidate-other"
            elif binding == "agent_version_id":
                row.agent_version_id = "candidate-other"
            elif binding == "candidate_worktree_path":
                payload["candidate_worktree_path"] = "/tmp/other-worktree"
            elif binding == "dataset_id":
                change_set_row = db.get(AgentChangeSetModel, change_set_id)
                assert change_set_row is not None
                change_set_payload = dict(change_set_row.payload_json or {})
                change_set_payload["regression_dataset_id"] = "tds-other"
                change_set_row.payload_json = change_set_payload
            elif binding == "source":
                row.source = "manual_feedback_dataset"
            elif binding == "complete_items":
                item = db.get(EvalRunItemModel, str(eval_run["items"][0]["eval_run_item_id"]))
                assert item is not None
                db.delete(item)
            row.payload_json = payload

    with pytest.raises((ConflictError, DataIntegrityError)):
        governance.complete_regression(
            change_set_id,
            eval_run_id=eval_run_id,
            operator="tester",
        )

    current = governance.get_change_set(change_set_id)
    assert current is not None and current["status"] == "regression_running"


def test_stale_regression_owner_cannot_overwrite_retry(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set, intent_id="evr-intent-old")
    dataset_id = str(eval_run["dataset_id"])
    governance.fail_regression(
        change_set_id,
        expected_eval_run_id="evr-intent-old",
        error_type="RuntimeError",
        operator="tester",
    )
    failed = governance.get_change_set(change_set_id)
    assert failed is not None
    assert failed["latest_eval_run_id"] == eval_run["eval_run_id"]
    assert failed["latest_eval_run"] == governance.feedback_store.get_eval_run(str(eval_run["eval_run_id"]))
    governance.mark_regression_running(
        change_set_id,
        eval_run_id="evr-intent-new",
        dataset_id=dataset_id,
        operator="tester",
    )

    with pytest.raises(ConflictError, match="regression_attempt_id does not match"):
        governance.complete_regression(
            change_set_id,
            eval_run_id=str(eval_run["eval_run_id"]),
            operator="stale-runner",
        )

    current = governance.get_change_set(change_set_id)
    assert current["status"] == "regression_running"
    assert current["latest_eval_run_id"] == "evr-intent-new"


def test_force_publish_failed_regression_records_audit_event(tmp_path):
    """P4 发布门禁：普通发布被失败回归阻断；force=True 才能发布并留下强制审计。"""
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    eval_run = _persist_regression_run(governance, change_set, item_status="failed")
    governance.complete_regression(
        change_set_id,
        eval_run_id=str(eval_run["eval_run_id"]),
        operator="tester",
    )

    with pytest.raises(AgentGovernanceError):
        governance.publish_change_set(change_set_id, operator="tester")

    release = governance.publish_change_set(change_set_id, operator="lead", note="人工确认风险可接受", force=True)
    persisted = governance.get_change_set(change_set_id)

    assert release["status"] == "published"
    assert persisted["status"] == "published"
    assert persisted["force_published"] is True
    assert "回归验证存在失败用例" in persisted["force_publication_blocker"]
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

    governance.request_change_set_approval(change_set_id, operator="reviewer", reason="风险过高", impact_scope="工具配置", rollback_plan="撤销变更")
    rejected = governance.reject_change_set(change_set_id, operator="reviewer", note="不通过")

    assert rejected["status"] == "rejected"
    actions = {str(event.get("action")) for event in governance.list_change_set_events(change_set_id)}
    assert {"approval_requested", "rejected"} <= actions


def test_repository_ops_route_per_agent_not_always_main(tmp_path):
    """缺陷②回归：repository_status/snapshot/current_ref 按 agent_id 路由到对应 per-agent 版本库，
    不再恒走 main 主库（per-agent 版本治理隔离）。"""
    governance, main_store = _governance(tmp_path)
    # main-agent 仍走传入的主库实例。
    # main 不再预置为传入的那个 store 实例（预置会在 main 被删除后留下悬空 store）；
    # 它与其他业务 Agent 一样懒建，但仍指向同一个 workspace 版本库。
    assert governance._store_for("main-agent").repository_dir == main_store.repository_dir
    # 业务 Agent 走独立 per-agent 库：不同实例、不同 repository_dir。
    biz_store = governance._store_for("biz-x")
    assert biz_store.repository_dir != main_store.repository_dir
    assert main_store.repository_dir != biz_store.repository_dir
    assert "business-agents/biz-x/workspace" in str(biz_store.repository_dir)
    # repository_status 按 agent_id 路由：业务 Agent 的状态来自其自己的库，不是主库。
    biz_status = governance.repository_status("biz-x")
    main_status = governance.repository_status("main-agent")
    assert str(biz_store.repository_dir) == str(biz_status["repository_dir"])
    assert biz_status["repository_dir"] != main_status["repository_dir"]


def test_version_governance_rejects_unregistered_ghost_agent(tmp_path):
    """缺陷④：装配 agent_exists 后，未注册 agent_id 的版本治理操作被拒（404），不懒建幽灵版本库。

    main-agent 不再豁免这条校验：它是可删除的普通业务 Agent，删除后对它的版本治理请求应当
    404，而不是就地重建一个版本库把它复活。
    """
    governance, _ = _governance(tmp_path)
    governance.agent_exists = lambda aid: aid in {"real-biz", "main-agent"}
    with pytest.raises(AgentGovernanceError) as exc:
        governance.repository_status("ghost-agent")
    assert exc.value.status_code == 404
    # 已注册的放行（main-agent 与其他业务 Agent 同等对待）。
    assert governance.repository_status("main-agent")
    assert governance.repository_status("real-biz")

    # main-agent 未注册（已删除）时同样 404——没有「恒有效」豁免。
    governance.evict_agent_store("main-agent")
    governance.agent_exists = lambda aid: aid == "real-biz"
    with pytest.raises(AgentGovernanceError) as deleted_main:
        governance.repository_status("main-agent")
    assert deleted_main.value.status_code == 404
