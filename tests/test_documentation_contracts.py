from pathlib import Path

from scripts.export_openapi import build_openapi_schema

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _assert_contains_all(text: str, markers: tuple[str, ...]) -> None:
    for marker in markers:
        assert marker in text


def _assert_contains_none(text: str, markers: tuple[str, ...]) -> None:
    for marker in markers:
        assert marker not in text


def test_readme_api_index_uses_current_improvement_and_agent_routes():
    readme = _read_repo_text("README.md")

    # README 只负责把读者路由到契约真相源，不维护会随实现变化的逐路由索引。
    _assert_contains_all(
        readme,
        (
            "docs/README.md",
            "docs/AgentGov集成指南.md",
            "docs/engineering/部署与运行手册.md",
        ),
    )
    _assert_contains_none(
        readme,
        (
            "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
            "/api/optimization-proposals",
            "/api/feedback-optimization-batches",
            "/api/optimization-tasks",
            "/api/improvements/{improvement_id}/attribution/generate",
            "/api/improvements/{improvement_id}/optimization-plan/generate",
            "/api/improvements/{improvement_id}/execution/apply",
            "/api/improvements/{improvement_id}/regression-test-design/generate",
            "/api/langfuse/traces/{trace_id}",
            "/api/agent-change-sets/{change_set_id}/publish",
        ),
    )


def test_runtime_raw_event_docs_distinguish_byte_stream_from_legacy_sdk_projection():
    readme = _read_repo_text("README.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")

    # 特权诊断接口的完整 wire/security 语义只由集成指南和 OpenAPI 承担。
    _assert_contains_all(
        guide,
        (
            "/api/debug/agent-runtime/raw-events",
            "ENABLE_AGENT_RUNTIME_RAW_EVENTS",
            "AGENT_RUNTIME_RAW_EVENTS_MAX_BYTES",
            "application/octet-stream",
            "byte-exact",
            "event_mode=raw",
            "不是 Anthropic-compatible provider HTTP wire",
            "`agentgov.debug.sdk_raw` 是历史的已解析 SDK 投影",
        ),
    )
    assert "docs/AgentGov集成指南.md" in readme
    _assert_contains_none(
        readme,
        (
            "/api/debug/agent-runtime/raw-events",
            "ENABLE_AGENT_RUNTIME_RAW_EVENTS",
            "AGENT_RUNTIME_RAW_EVENTS_MAX_BYTES",
            "event_mode=raw",
            "byte-exact",
        ),
    )


def test_docs_separate_playground_sdk_native_from_chat_and_responses_projection():
    readme = _read_repo_text("README.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")
    adr = _read_repo_text("docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md")

    _assert_contains_all(
        guide,
        (
            "/api/agent-runtime/sdk-events",
            "claude.sdk.<ClassName>",
            "live turn 只调用该入口",
        ),
    )
    _assert_contains_all(
        adr,
        (
            "/api/agent-runtime/sdk-events",
            "claude.sdk.<ClassName>",
            "不继承或包装 Responses projector",
        ),
    )
    assert "docs/AgentGov集成指南.md" in readme
    _assert_contains_none(readme, ("claude.sdk.<ClassName>", "Responses projector"))


def test_public_integration_docs_expose_transitional_responses_and_single_hitl_surface():
    readme = _read_repo_text("README.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")
    adr = _read_repo_text("docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md")
    skill = _read_repo_text("integrations/agentgov-integration/SKILL.md")

    _assert_contains_all(
        guide,
        (
            "/api/agent-runtime/sdk-events",
            "/v1/conversations",
            "/v1/agentgov/confirmation-requests/{request_id}/decision",
            "过渡",
            "id=null",
            "metadata",
        ),
    )
    assert "docs/AgentGov集成指南.md" in readme
    _assert_contains_none(
        readme,
        (
            "/v1/agentgov/confirmation-requests/{request_id}/decision",
            "id=null",
        ),
    )

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

    # 首页按读者任务组织，不再把仓库目录树当成稳定产品契约。
    for section in (
        "为什么 AgentGov",
        "能力状态",
        "五分钟启动",
        "核心工作流",
        "架构边界",
        "文档导航",
        "安全与适用边界",
        "项目治理",
    ):
        assert f"## {section}" in readme
    assert "## 目录结构" not in readme

    linked_docs = (
        "docs/README.md",
        "docs/AgentGov集成指南.md",
        "docs/engineering/部署与运行手册.md",
    )
    for path in linked_docs:
        assert path in readme
        assert (REPO_ROOT / path).is_file(), f"README 导航目标不存在: {path}"


def test_public_docs_distinguish_completed_worktree_phase_from_current_release():
    state = _read_repo_text(".planning/STATE.md")
    readme = _read_repo_text("README.md")
    baseline = _read_repo_text("docs/反馈闭环当前实现基线.md")
    guide = _read_repo_text("docs/AgentGov集成指南.md")
    version = _read_repo_text("VERSION").strip()

    assert "Phase 7 complete" in state
    assert f"`{version}`" in readme
    _assert_contains_all(readme, ("Phase 7 已完成同候选验收", "尚未随当前发布版本发布"))
    _assert_contains_all(
        baseline,
        ("Phase 7 工作树已验证，尚未发布", "不证明 P0-MCP"),
    )
    _assert_contains_all(
        guide,
        ("Phase 7 已在同一 v3.1 候选上完成", "尚未随当前发布版本"),
    )

    stale_phase_claims = (
        "Phase 7 为“已实现待终验”",
        "串行 `make test` 与适用的真实容器门仍待完成",
        "隔离 lane 已实现但仍处于最终验收阶段",
    )
    for text in (readme, baseline, guide):
        _assert_contains_none(text, stale_phase_claims)


def test_active_docs_do_not_restore_removed_optimization_batch_or_eval_chain():
    terms = _read_repo_text("docs/AgentGov术语与版本边界.md")
    cases = _read_repo_text("docs/AgentGov核心功能测试用例.md")

    _assert_contains_all(
        terms,
        (
            "已删除旧链路中合并反馈并生成方案的容器",
            "已删除旧链路中的方案对象和 job 输出",
            "当前执行证据使用 `AgentTestRun`",
        ),
    )
    _assert_contains_none(
        terms,
        (
            "当前多条反馈合并生成方案的容器",
            "当前方案生成 job 的输出命名",
            "当前代码/API 名可保留",
        ),
    )
    _assert_contains_none(
        cases,
        (
            "提交更新后的待发布版本并自动排队运行",
            "run→反馈→优化批次→评估→change set→release",
            "反馈错误关联到 Agent B 的优化批次",
        ),
    )


def test_core_agv_statuses_match_current_openapi_and_evaluation_gap():
    cases = _read_repo_text("docs/AgentGov核心功能测试用例.md")
    schema = build_openapi_schema()
    paths = set(schema["paths"])

    def section(case_id: str, next_case_id: str) -> str:
        return cases.split(f"### {case_id}", 1)[1].split(f"### {next_case_id}", 1)[0]

    assert "状态：`current`" in section("AGV-007", "AGV-008")
    assert "evaluator-owned 独立测评仍是规划能力" in section("AGV-007", "AGV-008")
    assert "状态：`gap`" in section("AGV-017", "AGV-018")
    assert "没有生成归因、优化或独立评测对象" in section("AGV-017", "AGV-018")
    assert "状态：`gap`" in section("AGV-022", "AGV-023")
    assert "尚未" in section("AGV-022", "AGV-023")
    assert "状态：`gap`" in section("AGV-025", "AGV-026")
    agv_020 = section("AGV-020", "AGV-021")
    assert "当前创建态为 active" in agv_020
    assert "创建 draft Agent" not in agv_020
    assert "状态：`gap`" in section("AGV-041", "AGV-042")
    assert "没有生产" in section("AGV-041", "AGV-042")
    assert "状态：`current`" in section("AGV-050", "AGV-051")
    assert "状态：`gap`" in section("AGV-051", "AGV-052")

    assert {
        "/api/agent-runtime/sdk-events",
        "/api/feedback-cases",
        "/api/improvements",
        "/api/agent-test-runs",
        "/api/agent-change-sets",
    } <= paths
    assert not any(segment in path for path in paths for segment in ("/eval-runs", "/assessments", "/evaluation-benchmarks"))


def test_speech_summary_adr_marks_old_gap_matrix_as_completed_history():
    adr = _read_repo_text("docs/engineering/Agent运行时语义事件与SpeechSummary整改方案.md")

    _assert_contains_all(
        adr,
        (
            "落地状态：3.0.3 已实现",
            "整改前问题基线（2026-07-28）",
            "原处置",
            "不是当前完成度",
            "已验证：专用请求 schema、canonical envelope、done 最后",
            "已验证：OpenAPI + docs，无 sunset 日期",
        ),
    )
    assert "### 2.1 当前问题" not in adr


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
    deployment = _read_repo_text("docs/engineering/部署与运行手册.md")
    test_governance = _read_repo_text("docs/engineering/测试资产组合治理.md")
    core_cases = _read_repo_text("docs/AgentGov核心功能测试用例.md")

    assert "docs/engineering/部署与运行手册.md" in readme
    _assert_contains_all(
        deployment,
        (
            "当前工作树",
            "--force-recreate",
            "make container-core-smoke",
            "make container-live-test",
            "make container-workspace-pytest-test",
        ),
    )
    orchestration_markers = (
        "profile → verifier",
        "候选 Git tree",
        "selected env",
        "caller env scrub",
        "fixed toolchain",
        "受管 browser",
        "reserved -> prepared",
        "lifecycle lock",
        "stale recovery",
        "terminal signal barrier",
        "AgentTestExecutionReceipt",
    )
    _assert_contains_all(test_governance, orchestration_markers)
    _assert_contains_none(readme, orchestration_markers)

    active_docs = (
        readme,
        deployment,
        test_governance,
        _read_repo_text("docs/AgentGov集成指南.md"),
        _read_repo_text("docs/engineering/业务AgentWorkspace原生pytest测试资产实现方案.md"),
        _read_repo_text("docs/业务AgentWorkspace包导入与热加载产品工程方案.md"),
        _read_repo_text("docs/反馈闭环当前实现基线.md"),
    )
    for text in active_docs:
        _assert_contains_none(
            text,
            (
                "RUNTIME_BOOTSTRAP_HOST_DIR",
                "./runtime-bootstrap:/app/docker/runtime-bootstrap:ro",
                "不需要重建镜像",
            ),
        )

    assert "最多三路并行" in test_governance
    assert "main-full` 保持串行" in test_governance
    assert "make ui-openai-responses-smoke" in core_cases
    assert "pnpm --dir frontend run verify:openai-responses-container" not in core_cases


def test_workspace_pytest_docs_match_the_isolated_sandbox_contract():
    readme = _read_repo_text("README.md")
    docs_index = _read_repo_text("docs/README.md")
    implementation = _read_repo_text("docs/engineering/业务AgentWorkspace原生pytest测试资产实现方案.md")

    assert "docs/engineering/测试资产组合治理.md" in readme
    assert "业务AgentWorkspace原生pytest测试资产实现方案.md" in docs_index

    sandbox_markers = (
        "/usr/local/bin/python -I -P -m pytest -q --import-mode=importlib -p agentgov_testkit.pytest_plugin tests",
        "make container-workspace-pytest-test",
        "/workspace:ro",
        "/output",
        "/tmp",
        "tmpfs",
        "stdout envelope",
        "tail",
        "独立临时",
        "Compose project",
        "不等于独立证明业务正确性",
        "assurance_level=execution_provenance",
        "workspace_report_authority=agent_owned_unverified",
        "receipt.result.workspace_report_authority",
        "named volume",
        "候选 Git tree",
        "materialized snapshot",
    )
    _assert_contains_all(implementation, sandbox_markers)
    _assert_contains_none(
        readme,
        (
            sandbox_markers[0],
            "/workspace:ro",
            "stdout envelope",
            "assurance_level=execution_provenance",
            "workspace_report_authority=agent_owned_unverified",
            "receipt.result.workspace_report_authority",
            "named volume",
            "materialized snapshot",
        ),
    )
    assert "RUNTIME_BOOTSTRAP_HOST_DIR" not in implementation


def test_workspace_durable_docs_remove_legacy_operation_contract() -> None:
    active_docs = (
        _read_repo_text("README.md"),
        _read_repo_text("docs/反馈闭环当前实现基线.md"),
        _read_repo_text("docs/业务AgentWorkspace包导入与热加载产品工程方案.md"),
        _read_repo_text("docs/engineering/业务AgentWorkspace激活故障恢复Runbook.md"),
    )
    stale_claims = (
        "无需持久化第二套 operation 状态",
        "Operation state is not persisted",
        "删除完成后可重建同 ID",
    )
    for text in active_docs:
        _assert_contains_none(text, stale_claims)


def test_workspace_activation_docs_match_durable_contract() -> None:
    readme = _read_repo_text("README.md")
    product = _read_repo_text("docs/业务AgentWorkspace包导入与热加载产品工程方案.md")
    baseline = _read_repo_text("docs/反馈闭环当前实现基线.md")

    for migration in ("0055", "0056", "0057", "0058"):
        assert f"migration `{migration}`" in baseline
        assert f"migration {migration}" not in readme
        assert f"migration `{migration}`" not in readme

    _assert_contains_all(
        product,
        (
            "completion_outcome",
            "rejection_outcome",
            "recovery_required",
            "admission tuple",
            "HEAD/index/Workspace 字节",
            "durable refs",
            "graph identity",
            "assume-unchanged",
            "skip-worktree",
            "fsmonitor flag",
            "GIT_*",
            "--git-dir",
            "--work-tree",
            "object alternates",
            "commondir",
            "grafts",
            "partial clone/promisor",
            "raw commit object header",
            "direct refs",
            "HEAD topology",
        ),
    )


def test_workspace_deletion_docs_match_durable_contract() -> None:
    paths = set(build_openapi_schema()["paths"])
    product = _read_repo_text("docs/业务AgentWorkspace包导入与热加载产品工程方案.md")

    assert "/api/agent-registry/{agent_id}" in paths
    assert "/api/agent-deletion-operations/{operation_id}" in paths
    _assert_contains_all(
        product,
        (
            "永久保留",
            "从未公开过的",
            "workspace_must_be_absent",
            "per-Agent lock",
            "同 UID 或 root",
        ),
    )


def test_workspace_operator_recovery_docs_match_durable_contract() -> None:
    docs_index = _read_repo_text("docs/README.md")
    runbook = _read_repo_text("docs/engineering/业务AgentWorkspace激活故障恢复Runbook.md")

    assert "docs/engineering/业务AgentWorkspace激活故障恢复Runbook.md" in docs_index
    _assert_contains_all(
        runbook,
        (
            "make workspace-activation-recovery",
            "WORKSPACE_ACTIVATION_RECOVERY_ARGS",
            "`list`",
            "`inspect`",
            "`apply`",
            "`resume`",
            "state_digest",
            "repair-missing-refs",
            "active_recovery_attempt.state=reserved",
            "RECOVERY_ATTEMPT_EVIDENCE_INVALID",
            "force-complete",
            "HTTP mutation",
            "archive command",
            "HEAD mutation",
        ),
    )
    assert "run-tool python -m app.runtime.recovery_cli_support" not in runbook


def test_active_docs_pin_workspace_filesystem_ui_and_phase8_boundaries() -> None:
    readme = _read_repo_text("README.md")
    product = _read_repo_text("docs/业务AgentWorkspace包导入与热加载产品工程方案.md")
    baseline = _read_repo_text("docs/反馈闭环当前实现基线.md")
    test_governance = _read_repo_text("docs/engineering/测试资产组合治理.md")

    workspace_authority_markers = (
        "migration 0059 trigger authority",
        "fd-relative bounded Workspace fingerprint",
        "fd-relative Git metadata/temp authority",
    )
    _assert_contains_all(product, workspace_authority_markers)
    assert "migration `0059`" in baseline
    _assert_contains_none(readme, workspace_authority_markers)
    _assert_contains_none(readme, ("Settings request-context", "late success/error/finally"))

    # 阶段完成度属于 planning/验收回执，不应固化进长期工程契约。
    _assert_contains_none(
        test_governance,
        (
            "Phase 7 当前完成候选",
            "Phase 8 / P0-MCP 仍待独立实施与验收",
            "当前候选的最终 `make test`",
            "同一候选公共容器验收尚未形成完成回执",
        ),
    )
