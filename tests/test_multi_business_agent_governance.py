"""#24：治理闭环 per-agent 化的单元验证——版本/执行目标/候选回归归属对非 main 业务 Agent 正确隔离。

覆盖：
- C/D：FeedbackStore._current_agent_version_id(agent_id) 按业务 Agent 路由其自身版本（不落 main）。
- B：执行目标 sha/workspace 按业务 Agent 解析——AAA 的 CLAUDE.md 与 main 不同则 sha 不同（旧实现拿 main sha 比 AAA → 409）。
- B：_agent_git_paths_context(agent_id) 仓库/worktrees/releases 路径落到该 Agent 自己的版本库。
- A：候选回归把 change_set.agent_id 透传给 run_candidate（不再写死 main-agent-candidate）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.runtime.agent_paths import business_agent_layout
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.feedback_eval_runner import FeedbackEvalRunner


def _make_store(tmp_path: Path) -> FeedbackStore:
    data_dir = tmp_path / "data"
    main_ws = business_agent_layout(data_dir, "main-agent").workspace
    aaa_ws = business_agent_layout(data_dir, "AAA").workspace
    main_ws.mkdir(parents=True, exist_ok=True)
    aaa_ws.mkdir(parents=True, exist_ok=True)
    (main_ws / "CLAUDE.md").write_text("main agent baseline config\n", encoding="utf-8")
    (aaa_ws / "CLAUDE.md").write_text("AAA agent OPTIMIZED config — different from main\n", encoding="utf-8")
    # provider 按 agent_id 路由（用 fake 版本号代表「各自的库 HEAD」）。
    return FeedbackStore(data_dir=data_dir, agent_version_provider=lambda aid: f"ver-{aid or 'main-agent'}")


def test_current_agent_version_id_routes_per_agent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store._current_agent_version_id("AAA") == "ver-AAA"
    assert store._current_agent_version_id("BBB") == "ver-BBB"
    assert store._current_agent_version_id("main-agent") == "ver-main-agent"
    assert store._current_agent_version_id() == "ver-main-agent"  # 缺省回退 main，行为不变


def test_execution_targets_and_sha_are_per_agent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    data_dir = tmp_path / "data"
    # workspace 解析按 agent_id：AAA 落 business-agents/AAA/workspace，main 复用主 policy。
    assert store._execution_targets_for("AAA").workspace_dir == business_agent_layout(data_dir, "AAA").workspace
    assert store._execution_targets_for("main-agent") is store.execution_targets
    assert store._execution_targets_for(None) is store.execution_targets
    # B 的核心：AAA 的 CLAUDE.md 内容与 main 不同 → sha 不同。旧实现恒用 main sha 比对 AAA worktree → 409。
    ctx_main = store._execution_target_file_context("CLAUDE.md", "main-agent")
    ctx_aaa = store._execution_target_file_context("CLAUDE.md", "AAA")
    assert ctx_main.get("sha256") and ctx_aaa.get("sha256")
    assert ctx_main["sha256"] != ctx_aaa["sha256"]


def test_agent_git_paths_context_is_per_agent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    data_dir = tmp_path / "data"
    aaa = store._agent_git_paths_context("AAA")
    assert aaa["main_agent_repository_path"] == str(business_agent_layout(data_dir, "AAA").workspace)
    assert aaa["agent_change_set_worktrees_path"] == str(business_agent_layout(data_dir, "AAA").version_base / "worktrees")
    main = store._agent_git_paths_context("main-agent")
    assert main["main_agent_repository_path"] == str(store.main_workspace_dir)
    assert main["agent_change_set_worktrees_path"] == str(business_agent_layout(data_dir, "main-agent").version_base / "worktrees")
    assert main["agent_release_archives_path"] == str(business_agent_layout(data_dir, "main-agent").version_base / "releases")
    # main 与 AAA 的仓库路径必须不同（否则 AAA 执行会落到 main 库）。
    assert aaa["main_agent_repository_path"] != main["main_agent_repository_path"]


def test_eval_runner_threads_change_set_agent_id_to_candidate() -> None:
    """A：候选回归把 change_set 归属的 agent_id 透传给 run_candidate（旧实现写死 main）。"""

    class _StubStore:
        def _resolve_eval_run_agent_id(self, change_set_id):  # 单一真相解析器（change_set.agent_id）
            return "AAA"

    captured: dict[str, object] = {}

    async def fake_run_candidate_chat(req, worktree, commit, change_set, agent_id):
        captured.update({"agent_id": agent_id, "change_set": change_set, "worktree": str(worktree)})
        return object()

    async def fake_run_chat(req):
        captured["fell_back_to_main"] = True
        return object()

    runner = FeedbackEvalRunner(
        feedback_store=_StubStore(),  # type: ignore[arg-type]
        run_chat=fake_run_chat,
        current_agent_version_id=lambda: None,
        run_candidate_chat=fake_run_candidate_chat,
    )
    asyncio.run(runner._run_eval_chat(object(), "cs-AAA-1", "candidate-sha", "/data/business-agents/AAA/version/worktrees/cs-AAA-1"))
    assert captured.get("agent_id") == "AAA"
    assert "fell_back_to_main" not in captured


def _record_agent_run(store, run_id: str, agent_id: str) -> None:
    store.record_run({
        "run_id": run_id, "session_id": f"s-{run_id}", "agent_id": agent_id, "message": "x",
        "messages": [{"event": "AssistantMessage", "content": [{"text": "ok"}]}],
        "created_at": "2026-05-20T00:00:00+00:00", "completed_at": "2026-05-20T00:00:01+00:00",
    })


def test_optimization_batch_agent_id_from_signal_not_main_default(tmp_path: Path) -> None:
    """#25：批次 agent_id 取自来源 signal 的 agent_id（来自 run.agent_id=AAA），不静默回退 main。"""
    from app.runtime.schemas import FeedbackSignalCreateRequest
    from tests.feedback_store_test_utils import _store as _full_store

    store, _ = _full_store(tmp_path)
    _record_agent_run(store, "run-aaa", "AAA")
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-aaa", labels=["tool_data_incomplete"]))
    assert signal["agent_id"] == "AAA"
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="AAA 批次")
    assert batch is not None and batch["agent_id"] == "AAA"


def test_optimization_batch_rejects_cross_agent(tmp_path: Path) -> None:
    """#25/AGV-025：跨 Agent（AAA+BBB）反馈混入同一批次被硬门拒绝，不污染他人版本治理。"""
    import pytest
    from app.runtime.errors import BusinessRuleViolation
    from app.runtime.schemas import FeedbackSignalCreateRequest
    from tests.feedback_store_test_utils import _store as _full_store

    store, _ = _full_store(tmp_path)
    _record_agent_run(store, "run-aaa", "AAA")
    _record_agent_run(store, "run-bbb", "BBB")
    sig_a = store.create_signal(FeedbackSignalCreateRequest(run_id="run-aaa", labels=["tool_data_incomplete"]))
    sig_b = store.create_signal(FeedbackSignalCreateRequest(run_id="run-bbb", labels=["tool_data_incomplete"]))
    with pytest.raises(BusinessRuleViolation):
        store.create_optimization_batch([
            {"source_kind": "signal", "source_id": sig_a["signal_id"]},
            {"source_kind": "signal", "source_id": sig_b["signal_id"]},
        ])
