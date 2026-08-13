from __future__ import annotations

from pathlib import Path

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.improvement_db import (
    AttributionModel,
    ExecutionRecordModel,
    ImprovementItemModel,
    OptimizationPlanModel,
)
from app.runtime.json_types import JsonObject
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import AgentChangeSetModel
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_change_set_provisioner import ChangeSetSource
from app.services.agent_governance import AgentGovernanceService

from business_agent_test_utils import create_test_business_agent_workspace
from feedback_store_test_utils import _settings


def _trusted_test_run(
    agent_id: str,
    commit_sha: str,
    *,
    test_run_id: str | None = None,
) -> JsonObject:
    return {
        "test_run_id": test_run_id or f"atr-{commit_sha[:12]}",
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "status": "passed",
        "suite_digest": "2" * 64,
        "source_digest": "3" * 64,
        "receipt": {"receipt_digest": "1" * 64},
    }


def _governance(tmp_path: Path) -> tuple[AgentGovernanceService, GitAgentVersionStore]:
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
    governance.latest_passed_test_run = lambda agent_id, commit_sha: _trusted_test_run(agent_id, commit_sha)
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
) -> JsonObject:
    if agent_id and agent_id != DEFAULT_BUSINESS_AGENT_ID:
        workspace = business_agent_layout(governance.feedback_store.data_dir, agent_id).workspace
        if not workspace.exists():
            create_test_business_agent_workspace(workspace, agent_id=agent_id, name=agent_id)
        governance._store_for(agent_id).ensure_bootstrap()
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
) -> tuple[JsonObject, str]:
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


def _bind_candidate_to_source_claim(
    governance: AgentGovernanceService,
    change_set: JsonObject,
    *,
    source_improvement_id: str,
    bound_at: str,
) -> None:
    with governance.feedback_store.Session.begin() as db:
        change_set_row = db.get(AgentChangeSetModel, str(change_set["change_set_id"]))
        assert change_set_row is not None
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
                change_set_id=str(change_set["change_set_id"]),
                status="confirmed",
                applied_agent_version_id=str(change_set["candidate_commit_sha"]),
                source_optimization_plan_id="opt-source-claim",
                source_optimization_plan_updated_at=bound_at,
                source_attribution_id="attr-source-claim",
                source_attribution_updated_at=bound_at,
            )
        )
        payload = dict(change_set_row.payload_json or {})
        payload.update(
            {
                "source_improvement_id": source_improvement_id,
                "source_attribution_id": "attr-source-claim",
            }
        )
        change_set_row.payload_json = payload


def _assert_improvement_release_completed(
    governance: AgentGovernanceService,
    release: JsonObject,
) -> None:
    with governance.feedback_store.Session() as db:
        completed_item = db.get(ImprovementItemModel, "imp-publish")

    assert release["source_improvement_id"] == "imp-publish"
    assert completed_item.improvement_stage == "release"
    assert completed_item.improvement_status == "done"
    assert completed_item.updated_at == release["updated_at"]
