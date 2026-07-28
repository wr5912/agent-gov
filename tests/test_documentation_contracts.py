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
