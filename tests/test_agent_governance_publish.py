from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.errors import ConflictError
from app.runtime.improvement_db import AttributionModel, ExecutionRecordModel, ImprovementItemModel, OptimizationPlanModel
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetCreateRequest,
    AgentChangeSetPublishRequest,
)
from app.runtime.runtime_db import (
    AgentChangeSetModel,
    AgentReleaseModel,
    AgentReleaseSourceClaimModel,
    AgentReleaseTagClaimModel,
    utc_now,
)
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_change_set_provisioner import ChangeSetSource
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService
from sqlalchemy.exc import OperationalError

from business_agent_test_utils import LEGACY_MAIN_AGENT_ID, ORDINARY_TEST_AGENT_ID, create_test_business_agent_workspace
from feedback_store_test_utils import _settings


def _governance(tmp_path):
    settings = _settings(tmp_path)
    agent_store = GitAgentVersionStore(
        repository_dir=settings.default_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    agent_store.ensure_bootstrap()
    store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.default_workspace_dir,
        agent_version_provider=lambda _aid=None: agent_store.current_version_id(),
    )
    governance = AgentGovernanceService(
        feedback_store=store,
        agent_version_store=agent_store,
        runtime_mode=settings.runtime_volume_mode,
        runtime_env={"MCP_SERVER_URL": "http://localhost:58001/mcp"},
    )
    governance.latest_passed_test_run = lambda agent_id, commit_sha: {
        "test_run_id": f"atr-{commit_sha[:12]}",
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "status": "passed",
    }
    # 默认业务 Agent 的版本库由夹具提前初始化；显式放进缓存，让测试注入失败或断言状态时
    # 与 service 懒建的实例保持同一对象。
    governance._agent_stores[DEFAULT_BUSINESS_AGENT_ID] = agent_store
    return governance, agent_store


def _candidate_change_set(
    governance: AgentGovernanceService,
    agent_store: GitAgentVersionStore,
    *,
    content: str = "# Test Agent\n\n发布候选变更。\n",
    agent_id: str | None = None,
):
    if agent_id and agent_id != DEFAULT_BUSINESS_AGENT_ID:
        workspace = business_agent_layout(governance.feedback_store.data_dir, agent_id).workspace
        if not workspace.exists():
            create_test_business_agent_workspace(workspace, agent_id=agent_id, name=agent_id)
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


def _feedback_candidate_change_set(
    governance: AgentGovernanceService,
    agent_store: GitAgentVersionStore,
) -> tuple[dict, str]:
    change_set_id = "agc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    bound_at = "2026-07-10T00:00:00+00:00"
    with governance.feedback_store.Session.begin() as db:
        db.add(
            ImprovementItemModel(
                improvement_id="imp-publish",
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
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
    committed = governance.mark_candidate_committed(
        change_set_id,
        candidate_commit_sha=candidate,
        execution_job_id="exec-publish",
    )
    return committed, bound_at


def test_stable_change_set_intent_is_idempotent_and_candidate_can_advance_before_publish(tmp_path):
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

    updated = governance.mark_candidate_committed(
        stable_id,
        candidate_commit_sha=stale_candidate,
        execution_job_id="exec-stable",
    )
    assert updated["candidate_commit_sha"] == stale_candidate
    assert updated["candidate_commit_sha"] != first_candidate
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
    """B3.1（AGV-017 版本维度基础）：change set/release 带默认业务 Agent ID 且可按 Agent 过滤。"""
    governance, agent_store = _governance(tmp_path)
    candidate = _candidate_change_set(governance, agent_store)
    assert candidate["agent_id"] == DEFAULT_BUSINESS_AGENT_ID
    assert all(cs["agent_id"] == DEFAULT_BUSINESS_AGENT_ID for cs in governance.list_change_sets())
    # 按 Agent 维度过滤 change set：默认 Agent 命中、其他 Agent 为空（不串扰）。
    assert governance.list_change_sets(agent_id=DEFAULT_BUSINESS_AGENT_ID)
    assert governance.list_change_sets(agent_id="biz-other") == []
    # 发布后 release 同样带 agent_id 且可按 Agent 过滤。
    published = governance.publish_change_set(str(candidate["change_set_id"]), operator="tester")
    assert published is not None
    assert all(rel["agent_id"] == DEFAULT_BUSINESS_AGENT_ID for rel in governance.list_releases())
    assert governance.list_releases(agent_id=DEFAULT_BUSINESS_AGENT_ID)
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
        change_set = _candidate_change_set(governance, default_store, content=f"# {agent_id}\n\n候选\n", agent_id=agent_id)
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
        assert {r["agent_id"] for r in store.list_runs(agent_id=agent_id)} == {agent_id}
        assert {s["agent_id"] for s in store.list_signals(agent_id=agent_id)} == {agent_id}
        assert records[agent_id]["case"]["agent_id"] == agent_id
        assert records[agent_id]["change_set"]["latest_test_run"]["agent_id"] == agent_id
        assert {c["agent_id"] for c in governance.list_change_sets(agent_id=agent_id)} == {agent_id}
        assert {rel["agent_id"] for rel in governance.list_releases(agent_id=agent_id)} == {agent_id}

    # 跨 Agent 隔离：alpha 的版本记录不出现在 beta 的过滤视图。
    alpha_cs = {c["change_set_id"] for c in governance.list_change_sets(agent_id="agent-alpha")}
    beta_cs = {c["change_set_id"] for c in governance.list_change_sets(agent_id="agent-beta")}
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


def test_publish_requires_passed_platform_test_for_exact_candidate_commit(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    agent_id = str(change_set["agent_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: None

    projected = governance.get_change_set(str(change_set["change_set_id"]))
    assert projected is not None
    assert projected["latest_test_run"] is None
    assert "commit_sha 完全匹配" in str(projected["publication_blocker"])
    with pytest.raises(AgentGovernanceError, match="commit_sha 完全匹配"):
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: {
        "test_run_id": "atr-wrong",
        "agent_id": agent_id,
        "commit_sha": "0" * 40,
        "status": "passed",
    }
    assert governance.get_change_set(str(change_set["change_set_id"]))["latest_test_run"] is None

    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: {
        "test_run_id": "atr-exact",
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "status": "passed",
    }
    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
    assert release["commit_sha"] == commit_sha


def test_force_publish_requires_reason_and_persists_warning_audit(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: None

    with pytest.raises(AgentGovernanceError, match="explicit reason") as exc:
        governance.publish_change_set(change_set_id, operator="tester", force=True)
    assert exc.value.status_code == 422

    reason = "紧急修复，已由值班负责人接受缺少平台测试的风险"
    release = governance.publish_change_set(
        change_set_id,
        operator="tester",
        note=reason,
        force=True,
    )
    assert release["force_published"] is True
    assert release["operator"] == "tester"
    assert release["force_publish_reason"] == reason
    assert release["force_publication_blocker"]
    events = governance.list_change_set_events(change_set_id)
    assert [event for event in events if event["action"] == "force_published"]

    assert AgentChangeSetPublishRequest(force=True, force_reason=reason).force_reason == reason
    with pytest.raises(ValueError, match="force_reason"):
        AgentChangeSetPublishRequest(force=True)


def test_feedback_publication_cannot_force_bypass_complete_agent_test_suite(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set, _bound_at = _feedback_candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    agent_id = str(change_set["agent_id"])
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: None

    projected = governance.get_change_set(change_set_id)
    assert projected is not None
    assert "commit_sha 完全匹配" in str(projected["publication_blocker"])
    with pytest.raises(AgentGovernanceError, match="完整 Agent 测试集.*不能强制绕过") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="tester",
            note="请求跳过失败测试",
            force=True,
        )
    assert exc.value.status_code == 409
    assert governance.get_change_set(change_set_id)["status"] == "candidate_committed"

    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: {
        "test_run_id": "atr-feedback-exact",
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "status": "passed",
    }
    release = governance.publish_change_set(change_set_id, operator="tester")
    assert release["commit_sha"] == commit_sha
    assert release["force_published"] is False


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
    source_improvement_id = "imp-source-claim"
    bound_at = utc_now()
    with governance.feedback_store.Session.begin() as db:
        first_row = db.get(AgentChangeSetModel, str(first["change_set_id"]))
        assert first_row is not None
        db.add(
            ImprovementItemModel(
                improvement_id=source_improvement_id,
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
                title="发布来源预留",
                improvement_stage="regression",
                improvement_status="active",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            AttributionModel(
                attribution_id="attr-source-claim",
                improvement_id=source_improvement_id,
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            OptimizationPlanModel(
                optimization_plan_id="opt-source-claim",
                improvement_id=source_improvement_id,
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            ExecutionRecordModel(
                execution_id="job-publish-test",
                improvement_id=source_improvement_id,
                change_set_id=str(first["change_set_id"]),
                status="confirmed",
                applied_agent_version_id=str(first["candidate_commit_sha"]),
                source_optimization_plan_id="opt-source-claim",
                source_optimization_plan_updated_at=bound_at,
                source_attribution_id="attr-source-claim",
                source_attribution_updated_at=bound_at,
            )
        )
        payload = dict(first_row.payload_json or {})
        payload.update(
            {
                "source_improvement_id": source_improvement_id,
                "source_attribution_id": "attr-source-claim",
            }
        )
        first_row.payload_json = payload

    def source_changed(*_args, **_kwargs):
        raise ConflictError("Source improvement changed during publication finalization")

    monkeypatch.setattr(finalization_module, "finalize_intent_source", source_changed)
    first_release = governance.publish_change_set(str(first["change_set_id"]), operator="tester")
    published_head = agent_store.current_commit_sha()
    assert first_release["source_improvement_id"] == source_improvement_id

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
        claim = db.get(AgentReleaseSourceClaimModel, (DEFAULT_BUSINESS_AGENT_ID, source_improvement_id))
        assert claim is not None and claim.change_set_id == first["change_set_id"]


def test_improvement_publication_rejects_unconfirmed_or_revised_provenance_even_with_force(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set, bound_at = _feedback_candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    candidate = str(change_set["candidate_commit_sha"])

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
        governance.publish_change_set(
            change_set_id,
            operator="tester",
            note="来源链已人工核验",
            force=True,
        )

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
        assert db.get(AgentReleaseTagClaimModel, (DEFAULT_BUSINESS_AGENT_ID, "release-branch-b")) is None


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
        "agent_id": DEFAULT_BUSINESS_AGENT_ID,
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
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
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

    governance.request_change_set_approval(change_set_id, operator="reviewer", reason="风险过高", impact_scope="工具配置", rollback_plan="撤销变更")
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
