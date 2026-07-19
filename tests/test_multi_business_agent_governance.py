"""#24：治理闭环 per-Agent 化的单元验证。

覆盖：
- C/D：FeedbackStore._current_agent_version_id(agent_id) 按业务 Agent 路由其自身版本。
- B：执行目标 sha/workspace 按业务 Agent 解析，普通 Agent 与平台默认 Agent 相互隔离。
- B：_agent_git_paths_context(agent_id) 仓库/worktrees/releases 路径落到该 Agent 自己的版本库。
- 版本与执行路径均按业务 Agent 隔离。
"""

from __future__ import annotations

from pathlib import Path

from app.runtime.agent_paths import business_agent_layout
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.stores.feedback_store import FeedbackStore


def _make_store(tmp_path: Path) -> FeedbackStore:
    data_dir = tmp_path / "data"
    default_ws = business_agent_layout(data_dir, DEFAULT_BUSINESS_AGENT_ID).workspace
    aaa_ws = business_agent_layout(data_dir, "AAA").workspace
    default_ws.mkdir(parents=True, exist_ok=True)
    aaa_ws.mkdir(parents=True, exist_ok=True)
    (default_ws / "CLAUDE.md").write_text("default agent baseline config\n", encoding="utf-8")
    (aaa_ws / "CLAUDE.md").write_text("AAA agent OPTIMIZED config - different from default\n", encoding="utf-8")
    # provider 按 agent_id 路由（用 fake 版本号代表「各自的库 HEAD」）。
    return FeedbackStore(
        data_dir=data_dir,
        agent_version_provider=lambda aid: f"ver-{aid or DEFAULT_BUSINESS_AGENT_ID}",
    )


def test_current_agent_version_id_routes_per_agent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store._current_agent_version_id("AAA") == "ver-AAA"
    assert store._current_agent_version_id("BBB") == "ver-BBB"
    assert store._current_agent_version_id("main-agent") == "ver-main-agent"
    assert store._current_agent_version_id() == f"ver-{DEFAULT_BUSINESS_AGENT_ID}"


def test_execution_targets_and_sha_are_per_agent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    data_dir = tmp_path / "data"
    # workspace 解析按 agent_id；只有未指定 ID 时使用平台默认 policy。
    assert store._execution_targets_for("AAA").workspace_dir == business_agent_layout(data_dir, "AAA").workspace
    assert store._execution_targets_for("main-agent") is not store.execution_targets
    assert store._execution_targets_for(None) is store.execution_targets
    # B 的核心：AAA 与默认 Agent 的配置内容不同，sha 也必须不同。
    ctx_default = store._execution_target_file_context("CLAUDE.md", DEFAULT_BUSINESS_AGENT_ID)
    ctx_aaa = store._execution_target_file_context("CLAUDE.md", "AAA")
    assert ctx_default.get("sha256") and ctx_aaa.get("sha256")
    assert ctx_default["sha256"] != ctx_aaa["sha256"]


def test_agent_git_paths_context_is_per_agent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    data_dir = tmp_path / "data"
    aaa = store._agent_git_paths_context("AAA")
    assert aaa["agent_repository_path"] == str(business_agent_layout(data_dir, "AAA").workspace)
    assert aaa["agent_change_set_worktrees_path"] == str(business_agent_layout(data_dir, "AAA").version_base / "worktrees")
    default = store._agent_git_paths_context(DEFAULT_BUSINESS_AGENT_ID)
    assert default["agent_repository_path"] == str(store.default_workspace_dir)
    assert default["agent_change_set_worktrees_path"] == str(business_agent_layout(data_dir, DEFAULT_BUSINESS_AGENT_ID).version_base / "worktrees")
    assert default["agent_release_archives_path"] == str(business_agent_layout(data_dir, DEFAULT_BUSINESS_AGENT_ID).version_base / "releases")
    assert aaa["agent_repository_path"] != default["agent_repository_path"]
