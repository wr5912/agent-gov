from pathlib import Path

from scripts.export_openapi import build_openapi_schema

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_readme_api_index_uses_current_improvement_and_agent_routes():
    readme = _read_repo_text("README.md")

    deprecated_routes = [
        "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
        "/api/optimization-proposals",
        "/api/feedback-optimization-batches",
        "/api/optimization-tasks",
    ]
    for route in deprecated_routes:
        assert route not in readme

    current_routes = [
        "/api/improvements",
        "/api/improvements/{improvement_id}/attribution/generate",
        "/api/improvements/{improvement_id}/optimization-plan/generate",
        "/api/improvements/{improvement_id}/execution/apply",
        "/api/improvements/{improvement_id}/regression-test-design/generate",
        "/api/langfuse/traces/{trace_id}",
        "/api/agent-change-sets/{change_set_id}/publish",
    ]
    for route in current_routes:
        assert route in readme


def test_runtime_raw_event_docs_distinguish_byte_stream_from_legacy_sdk_projection():
    readme = _read_repo_text("README.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")

    for text in (readme, guide):
        assert "/api/debug/agent-runtime/raw-events" in text
        assert "ENABLE_AGENT_RUNTIME_RAW_EVENTS" in text
        assert "AGENT_RUNTIME_RAW_EVENTS_MAX_BYTES" in text
        assert "application/octet-stream" in text
        assert "byte-exact" in text
        assert "event_mode=raw" in text
        assert "不是 Anthropic-compatible provider HTTP wire" in text
    assert "这里的 `raw` 是历史命名，不是 Claude Code CLI stdout" in readme
    assert "`agentgov.debug.sdk_raw` 是历史的已解析 SDK 投影" in guide


def test_docs_separate_playground_sdk_native_from_chat_and_responses_projection():
    readme = _read_repo_text("README.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")
    adr = _read_repo_text("docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md")

    for text in (readme, guide, adr):
        assert "/api/agent-runtime/sdk-events" in text
        assert "claude.sdk.<ClassName>" in text
    assert "不经 Chat 或 Responses 二次投影" in readme
    assert "live turn 只调用该入口" in guide
    assert "不继承或包装 Responses projector" in adr


def test_public_integration_docs_expose_transitional_responses_and_single_hitl_surface():
    readme = _read_repo_text("README.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")
    adr = _read_repo_text("docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md")
    skill = _read_repo_text("integrations/agentgov-integration/SKILL.md")

    for text in (readme, guide, adr, skill):
        assert "/api/agent-runtime/sdk-events" in text
        assert "/v1/conversations" in text
        assert "/v1/agentgov/confirmation-requests/{request_id}/decision" in text
        assert "过渡" in text

    for text in (readme, guide, adr):
        assert "id=null" in text
        assert "metadata" in text
        assert "下一次确认的破坏性版本" in text

    for stale_claim in ("≤16 对", "value≤512", "instructions 尽力回显"):
        assert stale_claim not in adr
    for stale_route in (
        "POST /api/claude-user-input-requests/{request_id}/decision",
        "/api/claude-hitl-requests",
        "/api/test-datasets",
        "/regression-runs",
    ):
        assert stale_route not in skill


def test_openapi_exposes_current_improvement_trace_routes_and_hides_legacy_optimization_chain():
    paths = set(build_openapi_schema()["paths"])

    legacy_paths = {
        "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
        "/api/optimization-proposals",
        "/api/feedback-optimization-batches",
        "/api/optimization-tasks/{optimization_task_id}/execution-jobs",
    }
    assert paths.isdisjoint(legacy_paths)

    current_paths = {
        "/api/improvements/{improvement_id}/attribution/generate",
        "/api/improvements/{improvement_id}/optimization-plan/generate",
        "/api/improvements/{improvement_id}/execution/apply",
        "/api/improvements/{improvement_id}/regression-test-design/generate",
        "/api/langfuse/traces/{trace_id}",
    }
    assert current_paths <= paths


def test_readme_directory_structure_matches_actual_repo_layout():
    readme = _read_repo_text("README.md")
    structure = readme.split("## 目录结构", 1)[1].split("## 快速启动", 1)[0]

    assert "runtime-bootstrap/" in structure
    bootstrap_root = REPO_ROOT / "docker" / "runtime-bootstrap"
    assert bootstrap_root.is_dir()
    for path in (
        "governor-workspace",
        "business-agents/security-operations-expert/workspace",
    ):
        assert (bootstrap_root / path).is_dir(), f"运行卷初始化源缺少 {path}"
    assert not (bootstrap_root / "templates").exists()

    tree_block = structure.split("```text", 1)[1].split("```", 1)[0]
    assert "volume/" not in tree_block
    assert "${HOME}/volume-agent-gov" in structure


def test_project_level_docs_and_skills_do_not_embed_business_agent_behavior():
    project_surfaces = (
        "README.md",
        "docs/AgentGov集成指南.md",
        ".codex/skills/business-agent-workspace-optimizer/SKILL.md",
        ".claude/skills/business-agent-workspace-optimizer/SKILL.md",
    )
    agent_specific_markers = (
        "soc_api__",
        "mcp__sec-ops",
        "response-playbook",
        "threat-response-disposition",
        "security-operations-analysis",
        "RO lifecycle",
        "control scope",
        "daily-secops",
    )

    for path in project_surfaces:
        text = _read_repo_text(path)
        for marker in agent_specific_markers:
            assert marker not in text, f"项目级入口 {path} 不得复制业务 Agent 专属标记 {marker}"


def test_container_acceptance_docs_require_fresh_current_worktree_and_public_targets():
    readme = _read_repo_text("README.md")
    integration = _read_repo_text("docs/AgentGov集成指南.md")
    test_governance = _read_repo_text("docs/engineering/测试资产组合治理.md")
    core_cases = _read_repo_text("docs/AgentGov核心功能测试用例.md")

    for text in (readme, integration, test_governance):
        assert "当前工作树" in text
        assert "--force-recreate" in text
        assert "公开" in text
    assert "make container-core-smoke" in readme
    assert "最多三路并行" in test_governance
    assert "main-full` 保持串行" in test_governance
    assert "make ui-openai-responses-smoke" in core_cases
    assert "pnpm --dir frontend run verify:openai-responses-container" not in core_cases
