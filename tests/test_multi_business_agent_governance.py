"""#24：两个非 main 业务 Agent 的真实 Git + SQLite 治理闭环隔离。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.business_agent_workspace import seed_business_agent_workspace
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import BusinessRuleViolation, DataIntegrityError
from app.runtime.schemas import ChatRequest, ChatResponse
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_content_store import ExecutionRecord, ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.runtime.stores.test_dataset_store import TestDatasetStore
from app.runtime.test_dataset_schemas import TestDatasetRecord
from app.services.agent_governance import AgentGovernanceService
from app.services.feedback_eval_runner import FeedbackEvalRunner
from app.services.improvement_execution_service import ImprovementExecutionService
from app.services.workspace_execution_applier import WorkspaceExecutionApplier

from feedback_store_test_utils import _settings

_AGENT_IDS = ("AAA", "BBB")
_MAIN_AGENT_ID = "main-agent"


class _ExecutionGovernor:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.contexts: dict[str, dict[str, object]] = {}

    async def __call__(self, **kwargs: Any) -> dict[str, object]:
        contexts = list(kwargs["job_input"]["target_file_contexts"])
        claude_md = next(item for item in contexts if item["path"] == "CLAUDE.md")
        source = str(claude_md["content_text"])
        agent_id = next(agent for agent in _AGENT_IDS if f"owner={agent}" in source)
        workspace_root = str(kwargs["job_input"]["target_policy"]["workspace_root"])
        assert Path(workspace_root).is_relative_to(business_agent_layout(self.data_dir, agent_id).version_base)
        self.contexts[agent_id] = {"sha256": claude_md["sha256"], "workspace_root": workspace_root}
        kwargs["trace_callback"]({"trace_id": f"trace-{agent_id}", "trace_url": f"https://trace.invalid/{agent_id}"})
        return {
            "status": "ready",
            "summary": f"已生成 {agent_id} 候选配置",
            "risk": "low",
            "operations": [
                {
                    "operation": "replace_file",
                    "path": "CLAUDE.md",
                    "content": _optimized_text(agent_id),
                    "expected_sha256": claude_md["sha256"],
                }
            ],
        }


class _CandidateRuntimeProbe:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.profiles: dict[str, Any] = {}

    async def run(self, req: ChatRequest, *, profile: Any, agent_version_id_override: str) -> ChatResponse:
        self.profiles[profile.agent_id] = profile
        return ChatResponse(
            run_id=f"run-{profile.agent_id}",
            session_id=req.session_id or "",
            agent_version_id=agent_version_id_override,
            answer=f"{profile.agent_id} candidate response",
        )

    async def run_candidate_chat(
        self,
        req: ChatRequest,
        worktree_path: Path,
        candidate_commit_sha: str,
        change_set_id: str,
        agent_id: str,
    ) -> ChatResponse:
        return await ClaudeRuntime.run_candidate(
            self,  # type: ignore[arg-type]
            req,
            worktree_path=worktree_path,
            candidate_commit_sha=candidate_commit_sha,
            change_set_id=change_set_id,
            agent_id=agent_id,
        )


@dataclass
class _Harness:
    settings: AppSettings
    feedback: FeedbackStore
    improvements: ImprovementStore
    content: ImprovementContentStore
    datasets: TestDatasetStore
    governance: AgentGovernanceService
    main_store: GitAgentVersionStore
    execution: ImprovementExecutionService
    governor: _ExecutionGovernor
    runtime: _CandidateRuntimeProbe
    baseline_shas: dict[str, str] = field(default_factory=dict)
    improvement_ids: dict[str, str] = field(default_factory=dict)


def _baseline_text(agent_id: str) -> str:
    return f"# {agent_id} BASELINE\nowner={agent_id}\n"


def _optimized_text(agent_id: str) -> str:
    return f"# {agent_id} OPTIMIZED\nowner={agent_id}\n"


def _git_store(settings: AppSettings) -> GitAgentVersionStore:
    return GitAgentVersionStore(
        repository_dir=settings.main_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )


def _new_governance(feedback: FeedbackStore, main_store: GitAgentVersionStore, settings: AppSettings) -> AgentGovernanceService:
    return AgentGovernanceService(
        feedback_store=feedback,
        agent_version_store=main_store,
        runtime_mode=settings.runtime_volume_mode,
        runtime_env={"MCP_SERVER_URL": "http://localhost:58001/mcp"},
    )


def _build_harness(tmp_path: Path) -> _Harness:
    settings = _settings(tmp_path)
    settings.main_workspace_dir.joinpath("CLAUDE.md").write_text(_baseline_text(_MAIN_AGENT_ID), encoding="utf-8")
    for agent_id in _AGENT_IDS:
        workspace = business_agent_layout(settings.data_dir, agent_id).workspace
        seed_business_agent_workspace(workspace, agent_id=agent_id, name=agent_id)
        workspace.joinpath("CLAUDE.md").write_text(_baseline_text(agent_id), encoding="utf-8")
    main_store = _git_store(settings)
    main_store.ensure_bootstrap()
    feedback = FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.main_workspace_dir)
    governance = _new_governance(feedback, main_store, settings)
    feedback.agent_version_provider = lambda agent_id: governance._store_for(agent_id).current_version_id()
    governor = _ExecutionGovernor(settings.data_dir)
    improvements = ImprovementStore(feedback.Session)
    content = ImprovementContentStore(feedback.Session)
    execution = ImprovementExecutionService(
        improvement_store=improvements,
        content_store=content,
        agent_governance=governance,
        execution_app=WorkspaceExecutionApplier(),
        run_profile_json=governor,
    )
    harness = _Harness(
        settings,
        feedback,
        improvements,
        content,
        TestDatasetStore(feedback.Session),
        governance,
        main_store,
        execution,
        governor,
        _CandidateRuntimeProbe(settings),
    )
    harness.baseline_shas[_MAIN_AGENT_ID] = str(main_store.current_commit_sha())
    for agent_id in _AGENT_IDS:
        harness.baseline_shas[agent_id] = str(governance._store_for(agent_id).current_commit_sha())
    return harness


def _seed_improvement_chain(harness: _Harness, agent_id: str) -> str:
    item = harness.improvements.create_improvement(agent_id=agent_id, title=f"{agent_id} 时间校验治理")
    harness.content.create_feedback(
        item.improvement_id,
        agent_id=agent_id,
        summary=f"{agent_id} 需要时间校验",
        raw_text=f"{agent_id} feedback",
        agent_version_id=harness.baseline_shas[agent_id],
    )
    harness.content.upsert_normalized_feedback(item.improvement_id, problem="缺少时间校验", advance_to_stage="triage")
    harness.content.set_normalized_feedback_status(item.improvement_id, status="confirmed")
    harness.content.upsert_attribution(item.improvement_id, summary="CLAUDE.md 约束不足", advance_to_stage="attribution")
    harness.content.set_attribution_status(item.improvement_id, status="confirmed")
    harness.content.upsert_optimization_plan(
        item.improvement_id,
        summary=f"仅优化 {agent_id} 的 CLAUDE.md",
        changes=[{"target": "CLAUDE.md", "change": "增加时间一致性约束"}],
        advance_to_stage="optimization",
    )
    harness.content.set_optimization_plan_status(item.improvement_id, status="confirmed")
    harness.improvement_ids[agent_id] = item.improvement_id
    return item.improvement_id


async def _execute_both(harness: _Harness) -> dict[str, ExecutionRecord]:
    records = await asyncio.gather(*(harness.execution.generate_and_apply_execution(harness.improvement_ids[agent_id]) for agent_id in _AGENT_IDS))
    return dict(zip(_AGENT_IDS, records, strict=True))


def _assert_candidate_isolation(harness: _Harness, executions: dict[str, ExecutionRecord]) -> None:
    for agent_id, execution in executions.items():
        change_set = harness.governance.get_change_set(execution.change_set_id)
        assert change_set is not None and change_set["agent_id"] == agent_id
        assert execution.applied_agent_version_id == change_set["candidate_commit_sha"]
        assert execution.base_commit_sha == harness.baseline_shas[agent_id]
        assert execution.generation_trace_id == f"trace-{agent_id}"
        worktree = Path(str(change_set["worktree_path"]))
        assert worktree.is_relative_to(business_agent_layout(harness.settings.data_dir, agent_id).version_base)
        assert worktree.joinpath("CLAUDE.md").read_text(encoding="utf-8") == _optimized_text(agent_id)
        workspace = business_agent_layout(harness.settings.data_dir, agent_id).workspace
        assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == _baseline_text(agent_id)
        assert harness.governance._store_for(agent_id).current_commit_sha() == harness.baseline_shas[agent_id]
        assert harness.feedback._current_agent_version_id(agent_id) == harness.baseline_shas[agent_id]
    assert executions["AAA"].change_set_id != executions["BBB"].change_set_id
    assert set(harness.governor.contexts) == set(_AGENT_IDS)
    _assert_main_unchanged(harness)


def _restart_governance(harness: _Harness) -> None:
    harness.governance = _new_governance(harness.feedback, harness.main_store, harness.settings)
    harness.feedback.agent_version_provider = lambda agent_id: harness.governance._store_for(agent_id).current_version_id()
    for agent_id in _AGENT_IDS:
        change_set_id = harness.content.get_execution(harness.improvement_ids[agent_id]).change_set_id  # type: ignore[union-attr]
        change_set = harness.governance.get_change_set(change_set_id)
        assert change_set is not None
        worktree = Path(str(change_set["worktree_path"]))
        assert harness.governance._store_for(agent_id).worktree_commit_sha(worktree) == change_set["candidate_commit_sha"]


def _prepare_active_dataset(harness: _Harness, agent_id: str) -> TestDatasetRecord:
    improvement_id = harness.improvement_ids[agent_id]
    harness.content.set_execution_status(improvement_id, status="confirmed", advance_to_stage="regression")
    harness.content.upsert_regression_assessment(
        improvement_id,
        summary=f"验证 {agent_id} 候选配置",
        cases=[
            {
                "prompt": f"请验证 {agent_id} 时间校验",
                "expected_behavior": "返回非空回答",
                "checkpoints": ["仅使用当前 Agent 候选版本"],
            }
        ],
    )
    harness.content.set_regression_assessment_status(improvement_id, status="confirmed")
    dataset = harness.datasets.adopt_from_improvement(improvement_id)
    assert dataset.provenance.baseline_agent_version_id == harness.baseline_shas[agent_id]
    return harness.datasets.transition_lifecycle(
        dataset.dataset_id,
        agent_id=agent_id,
        target_state="active",
        expected_revision=dataset.revision,
        operator="tester",
        reason="启用候选回归数据集",
    )


def _run_and_review_regression(harness: _Harness, agent_id: str, dataset: TestDatasetRecord) -> dict[str, object]:
    execution = harness.content.get_execution(harness.improvement_ids[agent_id])
    assert execution is not None
    change_set = harness.governance.get_change_set(execution.change_set_id)
    assert change_set is not None
    attempt_id = f"attempt-{agent_id}"
    harness.governance.mark_regression_running(
        execution.change_set_id,
        eval_run_id=attempt_id,
        dataset_id=dataset.dataset_id,
        operator="tester",
    )
    runner = FeedbackEvalRunner(
        feedback_store=harness.feedback,
        run_chat=lambda _req: _unexpected_main_runtime(),
        run_candidate_chat=harness.runtime.run_candidate_chat,
    )
    result = asyncio.run(
        runner.run_feedback_eval(
            dataset_id=dataset.dataset_id,
            source="agent_change_set_regression",
            change_set_id=execution.change_set_id,
            regression_attempt_id=attempt_id,
            candidate_commit_sha=execution.applied_agent_version_id,
            candidate_worktree_path=str(change_set["worktree_path"]),
        )
    )
    assert result is not None and result.agent_id == agent_id and result.result_status == "review_required"
    assert result.gate_result.status == "review_required" and {item.status for item in result.items} == {"needs_human_review"}
    awaiting_review = harness.governance.complete_regression(execution.change_set_id, eval_run_id=result.eval_run_id, operator="runner")
    assert awaiting_review["status"] == "regression_review_required"
    reviewed = harness.governance.review_regression(
        execution.change_set_id,
        eval_run_id=result.eval_run_id,
        review_id=f"review-{agent_id}",
        operator="reviewer",
        reason=f"已核验 {agent_id} 候选输出",
        scope="current_eval_run",
        items=[{"dataset_case_id": item.dataset_case_id, "decision": "approve", "note": "证据一致"} for item in result.items],
    )
    assert reviewed["result_status"] == "passed_with_notes"
    return reviewed


async def _unexpected_main_runtime() -> ChatResponse:
    raise AssertionError("candidate regression must not fall back to the main runtime")


def _publish_all(harness: _Harness) -> dict[str, dict[str, object]]:
    releases: dict[str, dict[str, object]] = {}
    for agent_id in _AGENT_IDS:
        execution = harness.content.get_execution(harness.improvement_ids[agent_id])
        assert execution is not None
        releases[agent_id] = harness.governance.publish_change_set(execution.change_set_id, operator="publisher")
        workspace = business_agent_layout(harness.settings.data_dir, agent_id).workspace
        assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == _optimized_text(agent_id)
        assert harness.governance._store_for(agent_id).current_commit_sha() == releases[agent_id]["commit_sha"]
        item = harness.improvements.get_improvement(harness.improvement_ids[agent_id])
        assert item is not None and (item.improvement_stage, item.improvement_status) == ("release", "done")
    _assert_main_unchanged(harness)
    return releases


def _assert_persisted_views_are_isolated(harness: _Harness) -> None:
    change_sets: dict[str, set[str]] = {}
    eval_runs: dict[str, set[str]] = {}
    for agent_id in _AGENT_IDS:
        assert {item.agent_id for item in harness.improvements.list_improvements(agent_id=agent_id)} == {agent_id}
        assert {item.agent_id for item in harness.content.list_feedbacks(harness.improvement_ids[agent_id])} == {agent_id}
        assert {item.agent_id for item in harness.datasets.list_datasets(agent_id=agent_id)} == {agent_id}
        assert {item["agent_id"] for item in harness.governance.list_releases(agent_id=agent_id)} == {agent_id}
        change_sets[agent_id] = {item["change_set_id"] for item in harness.governance.list_change_sets(agent_id=agent_id)}
        eval_runs[agent_id] = {item["eval_run_id"] for item in harness.feedback.list_eval_runs(agent_id=agent_id)}
        profile = harness.runtime.profiles[agent_id]
        assert profile.agent_id == agent_id and profile.langfuse_observation_name == f"runtime.candidate.{agent_id}"
        assert profile.workspace_dir.is_relative_to(business_agent_layout(harness.settings.data_dir, agent_id).version_base)
    assert change_sets["AAA"].isdisjoint(change_sets["BBB"])
    assert eval_runs["AAA"].isdisjoint(eval_runs["BBB"])
    assert harness.governance.list_change_sets(agent_id=_MAIN_AGENT_ID) == []
    assert harness.governance.list_releases(agent_id=_MAIN_AGENT_ID) == []


def _rollback_all_without_cross_talk(harness: _Harness, releases: dict[str, dict[str, object]]) -> None:
    rolled_aaa = harness.governance.rollback_release(str(releases["AAA"]["release_id"]), operator="rollback-tester")
    assert rolled_aaa["status"] == "rolled_back"
    assert business_agent_layout(harness.settings.data_dir, "AAA").workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == _baseline_text("AAA")
    assert business_agent_layout(harness.settings.data_dir, "BBB").workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == _optimized_text("BBB")
    assert harness.governance._store_for("BBB").current_commit_sha() == releases["BBB"]["commit_sha"]
    rolled_bbb = harness.governance.rollback_release(str(releases["BBB"]["release_id"]), operator="rollback-tester")
    assert rolled_bbb["status"] == "rolled_back"
    for agent_id in _AGENT_IDS:
        workspace = business_agent_layout(harness.settings.data_dir, agent_id).workspace
        assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == _baseline_text(agent_id)
        assert harness.governance._store_for(agent_id).current_commit_sha() == harness.baseline_shas[agent_id]
    _assert_main_unchanged(harness)


def _assert_main_unchanged(harness: _Harness) -> None:
    assert harness.settings.main_workspace_dir.joinpath("CLAUDE.md").read_text(encoding="utf-8") == _baseline_text(_MAIN_AGENT_ID)
    assert harness.main_store.current_commit_sha() == harness.baseline_shas[_MAIN_AGENT_ID]


def test_two_non_main_agents_complete_real_git_sqlite_governance_loop_without_cross_talk(tmp_path: Path) -> None:
    harness = _build_harness(tmp_path)
    for agent_id in _AGENT_IDS:
        _seed_improvement_chain(harness, agent_id)
    executions = asyncio.run(_execute_both(harness))
    _assert_candidate_isolation(harness, executions)
    _restart_governance(harness)
    for agent_id in _AGENT_IDS:
        dataset = _prepare_active_dataset(harness, agent_id)
        _run_and_review_regression(harness, agent_id, dataset)
    releases = _publish_all(harness)
    _assert_persisted_views_are_isolated(harness)
    _rollback_all_without_cross_talk(harness, releases)


def test_agent_ownership_boundaries_reject_missing_and_cross_agent_inputs(tmp_path: Path) -> None:
    store = FeedbackStore(data_dir=tmp_path / "data", agent_version_provider=lambda agent_id: f"version-{agent_id}")
    with pytest.raises(DataIntegrityError, match="missing valid business agent ownership"):
        store._current_agent_version_id(None)  # type: ignore[arg-type]
    with pytest.raises(DataIntegrityError, match="missing valid business agent ownership"):
        store._current_agent_version_id("../main-agent")

    def broken_provider(_agent_id: str) -> str:
        raise RuntimeError("version provider failed")

    store.agent_version_provider = broken_provider
    with pytest.raises(RuntimeError, match="version provider failed"):
        store._current_agent_version_id("AAA")
    improvements = ImprovementStore(store.Session)
    content = ImprovementContentStore(store.Session)
    item = improvements.create_improvement(agent_id="AAA", title="owner boundary")
    with pytest.raises(BusinessRuleViolation, match="across different business agents"):
        content.create_feedback(item.improvement_id, agent_id="BBB", summary="cross-agent feedback")
    probe = _CandidateRuntimeProbe(_settings(tmp_path / "runtime"))
    with pytest.raises(BusinessRuleViolation, match="does not match requested business agent"):
        asyncio.run(probe.run_candidate_chat(ChatRequest(message="missing owner"), tmp_path, "candidate", "agc-1", "AAA"))
    with pytest.raises(BusinessRuleViolation, match="does not match requested business agent"):
        asyncio.run(probe.run_candidate_chat(ChatRequest(message="wrong owner", agent_id="BBB"), tmp_path, "candidate", "agc-1", "AAA"))
