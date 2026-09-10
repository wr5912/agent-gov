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


def test_readme_documents_agent_scope_runtime_as_the_only_execution_path():
    readme = _read_repo_text("README.md")

    assert "agent-gov-api" in readme
    assert "agentscope-runtime" in readme
    assert "agent-gov-ui" in readme
    assert "/api/runtime/sessions/" in readme
    assert "/api/runtime/chat/" in readme
    assert "/api/runtime/sessions/{session_id}/stream" in readme
    assert "/api/agent-runs/{run_id}/trace" in readme
    assert "浏览器和外部调用方只访问" in readme

    for retired_surface in (
        "/api/agent-runtime/sdk-events",
        "/v1/responses",
        "/v1/conversations",
        "LiteLLM sidecar",
    ):
        assert retired_surface not in readme
    assert "A2UI 已退出镜像与运行依赖" in readme
    assert not (REPO_ROOT / "docker/vendor/A2UI").exists()


def test_operator_deployment_is_explicitly_single_tenant_and_loopback_by_default():
    readme = _read_repo_text("README.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")

    for document in (readme, guide):
        assert "单租户 operator" in document
        assert "跨用户" in document
        assert "loopback" in document
        assert "*_ALLOW_PUBLIC_BIND=1" in document
        assert "TLS" in document


def test_readme_documents_runtime_identifiers_and_trace_lookup():
    readme = _read_repo_text("README.md")

    for identifier in ("session_id", "run_id", "reply_id", "trace_id"):
        assert identifier in readme
    assert "这些标识用途不同，值不要求相同" in readme
    assert "GET /api/agent-runs/{run_id}/trace" in readme


def test_readme_documents_agent_scope_harness_and_controlled_publication():
    readme = _read_repo_text("README.md")

    for asset in (
        "agent.yaml",
        "AGENT.md",
        "skills/**/SKILL.md",
        "subagents/<name>/agent.yaml",
        "subagents/<name>/AGENT.md",
        "mcp/*.json",
    ):
        assert asset in readme
    assert "聊天运行不能直接修改自己的活动 Harness" in readme
    assert "已有会话继续绑定" in readme


def test_integration_guide_exposes_only_the_agentscope_runtime_journey():
    guide = _read_repo_text("docs/AgentGov集成指南.md")

    for route in (
        "POST /api/runtime/sessions/",
        "GET /api/runtime/sessions/{session_id}/stream",
        "POST /api/runtime/chat/",
        "GET /api/agent-runs/{run_id}",
        "GET /api/agent-runs/{run_id}/trace",
        "POST /api/agent-runs/{run_id}/cancel",
    ):
        assert route in guide
    for identifier in ("session_id", "run_id", "reply_id", "trace_id"):
        assert identifier in guide
    assert "AgentGov 已原子切换到 AgentScope Runtime" in guide
    assert "不再提供以下旧生产入口" in guide

    supported_contract = guide.split("## 8. 契约与部署验收", 1)[0]
    for retired_surface in (
        "/api/agent-runtime/sdk-events",
        "/v1/responses",
        "/v1/conversations",
        "Claude user-input",
        "LiteLLM sidecar",
    ):
        assert retired_surface not in supported_contract


def test_integration_skill_uses_the_exported_native_sse_extension_name():
    skill = _read_repo_text("integrations/agentgov-integration/SKILL.md")

    assert "x-agentgov-sse-contract" in skill
    assert "x-agentgov-sse-events" not in skill
    assert "未知事件" in skill


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
    test_governance = _read_repo_text("docs/engineering/测试资产组合治理.md")
    core_cases = _read_repo_text("docs/AgentGov核心功能测试用例.md")

    assert "当前工作树" in readme
    assert "--force-recreate" in readme
    assert "临时 Runtime 根" in readme
    assert "唯一 Compose project" in readme
    assert "随机回环端口" in readme
    assert "不会读写 `${HOME}/volume-agent-gov`" in readme
    assert "down --volumes" in readme
    assert "REQUIRE_LIVE_RUNTIME=1 make container-live-test" in readme
    assert "未执行前不得宣称" in readme
    assert "make container-core-smoke" in readme
    assert "make cutover-check" in readme
    assert "最多三路并行" in test_governance
    assert "main-full` 保持串行" in test_governance
    assert "不触碰既有部署" in test_governance
    assert "成功或失败" in core_cases
    assert "make ui-openai-responses-smoke" not in core_cases
    assert "make container-health-e2e" not in core_cases


def test_deployment_docs_split_safe_deploy_from_explicit_destructive_cutover():
    readme = _read_repo_text("README.md")

    assert "发现旧 Claude/未知 schema 时会在停服前 fail closed" in readme
    assert "绝不自动清空" in readme
    assert "PREPARE-AGENTSCOPE-FRESH-EPOCH" in readme
    for target in ("cutover-prepare", "cutover-execute", "cutover-restore", "cutover-finalize"):
        assert f"make {target}" in readme
    for safeguard in ("active run/HITL/test/publish", "restore drill", "inode", "snapshot hash", "不可逆点"):
        assert safeguard in readme


def test_runtime_docs_archive_all_retired_pre_agentscope_designs():
    docs_index = _read_repo_text("docs/README.md")
    archive_index = _read_repo_text("docs/archive/README.md")
    retired_names = (
        "Agent运行时语义事件与SpeechSummary整改方案.md",
        "vLLM模型网关与Sidecar整改优化方案.md",
        "多Runtime适配与外部CLI旁路及Multica协作边界方案.md",
        "AgentGov下一阶段P2ARuntime边界提取与ClaudeAdapter实施方案.md",
        "OpenAI兼容接口能否替代原生Chat端点评估.md",
        "AgentGov下一阶段P0准入收口实施方案.md",
        "AgentGov下一阶段P0模拟MCP平台验收实施方案.md",
    )

    assert "AgentScope Runtime 当前架构与公共契约" in docs_index
    assert "已取代 Claude/LiteLLM Sidecar、Speech Summary" in docs_index
    for name in retired_names:
        assert not (REPO_ROOT / "docs" / "engineering" / name).exists()
        assert (REPO_ROOT / "docs" / "archive" / "obsolete" / name).is_file()
        assert f"docs/archive/obsolete/{name}" in archive_index
