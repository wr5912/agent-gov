from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_readme_api_index_uses_current_feedback_and_agent_routes():
    readme = _read_repo_text("README.md")

    deprecated_routes = [
        "/api/agent-versions/main",
        "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
        "/api/optimization-proposals",
        "/api/feedback-analysis/jobs",
    ]
    for route in deprecated_routes:
        assert route not in readme

    current_routes = [
        "/api/feedback-cases/{feedback_case_id}/optimization-plan",
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/execute-all",
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/executions/{execution_run_id}/rollback",
        "/api/agent-repository/current",
        "/api/agent-change-sets/{change_set_id}/publish",
        "/api/agent-releases/{release_id}/restore",
        "/data/agent-governance/worktrees/",
        "/data/agent-governance/releases/",
    ]
    for route in current_routes:
        assert route in readme


def test_readme_directory_structure_matches_actual_repo_layout():
    """Issue #4：README「目录结构」必须与实际仓库布局一致，避免 docker/volume 这类漂移误导。"""
    readme = _read_repo_text("README.md")
    structure = readme.split("## 目录结构", 1)[1].split("## 快速启动", 1)[0]

    # 实际模板根是 docker/runtime-volume-seeds/，镜像运行卷初始态：governor 顶层种子 +
    # 预制业务 Agent（main 落 data/business-agents/main-agent/workspace）+ 创建模板 catalog。
    assert "runtime-volume-seeds/" in structure
    template_root = REPO_ROOT / "docker" / "runtime-volume-seeds"
    assert template_root.is_dir()
    assert "governor-workspace/" in structure, "README 结构缺少 governor-workspace"
    assert "business-agents/" in structure, "README 结构缺少预制业务 Agent（data/business-agents）"
    for path in (
        "governor-workspace",
        "data/business-agents/main-agent/workspace",
        "templates/business-agent",
    ):
        assert (template_root / path).is_dir(), f"模板缺少 {path}"

    # 目录树代码块不得把不存在的 docker/volume 子树当作当前布局展示。
    tree_block = structure.split("```text", 1)[1].split("```", 1)[0]
    assert "volume/" not in tree_block, "目录树不得把 docker/volume 当作当前布局"
    assert not (REPO_ROOT / "docker" / "volume").exists()

    # 运行态根目录说明保留（容器默认 ${HOME}/volume-agent-gov）。
    assert "${HOME}/volume-agent-gov" in structure


def test_readme_env_model_uses_single_file_modes_and_local_langfuse():
    readme = _read_repo_text("README.md")

    outdated_phrases = [
        "Langfuse Cloud",
        "https://cloud.langfuse.com",
        "https://us.cloud.langfuse.com",
        "http://langfuse.example.com",
        "额外读取不提交的 `docker/.env.local`",
        "容器部署私有覆盖",
        "不要把 host 调试路径写进 `docker/.env.local`",
        "Environment variables: RUNTIME_VOLUME_MODE=local-debug",
        "本机 PyCharm/uvicorn 调试使用单独的 `RUNTIME_VOLUME_MODE=local-debug`",
    ]
    for phrase in outdated_phrases:
        assert phrase not in readme

    current_phrases = [
        "Docker Compose 部署只读取 `docker/.env`",
        "本机 host/PyCharm 调试无需额外设置 `RUNTIME_VOLUME_MODE`",
        "宿主机 Python 进程会自动读取 `docker/.env.local-debug`",
        "应与 `docker/.env` 保持 Runtime/API/worker 应用配置同构",
        "AGENT_AUTH_REQUIRED",
        "Environment variables: 留空即可",
        "统一使用 `LOG_LEVEL` 控制应用日志级别",
        "启动日志会打印 `log_level`、`runtime_volume_mode`",
        "`provider_api_key_configured`",
        "LANGFUSE_BASE_URL=http://langfuse-web:3000",
        "LANGFUSE_NEXTAUTH_URL=http://localhost:53000",
        "FRONTEND_LANGFUSE_URL=http://localhost:53000",
    ]
    for phrase in current_phrases:
        assert phrase in readme


def test_codex_runtime_env_governance_routes_to_project_skill():
    override = _read_repo_text("AGENTS.override.md")
    project_rules = _read_repo_text(".codex/rules/project.rules")
    verify_rules = _read_repo_text(".codex/rules/verify.rules")
    codex_readme = _read_repo_text(".codex/README.md")
    skill = _read_repo_text(".codex/skills/runtime-env-governance/SKILL.md")
    optimizer_ref = _read_repo_text(".codex/skills/codex-config-optimizer/references/failure-analysis.md")

    for text in (override, project_rules, skill):
        assert "Consumer x Mode x Boundary" in text
        assert "runtime-env-governance" in text

    assert "不要把上述文件关系描述成“覆盖”" in override
    assert "不得把“被选择的 env 文件”描述成“覆盖”" in project_rules
    assert "Runtime/env 相关变更必须验证 settings 模式选择" in verify_rules
    assert "skills/runtime-env-governance/SKILL.md" in codex_readme
    assert "env-drift + surface-mismatch + execution-gap" in optimizer_ref


def test_feedback_current_baseline_replaces_archived_product_docs():
    doc = _read_repo_text("docs/反馈闭环当前实现基线.md")
    archive_index = _read_repo_text("docs/archive/README.md")
    docs_index = _read_repo_text("docs/README.md")

    archived_roots = [
        "docs/反馈优化产品调整方案.md",
        "docs/反馈优化闭环多智能体架构.md",
        "docs/反馈闭环机制全景画像.md",
    ]
    for path in archived_roots:
        assert not (REPO_ROOT / path).exists()
        assert path in archive_index
        assert path not in docs_index

    current_phrases = [
        "本文收敛并替代已归档的",
        "当前闭环仍以反馈信息、优化批次、回归资产、版本治理等 pre-v2.7 对象承载",
        "optimization batch / optimization plan",
        "optimization task / execution plan",
        "eval run / regression gate",
        "`change set` / `release`",
    ]
    for phrase in current_phrases:
        assert phrase in doc


def test_agent_version_plan_current_api_section_uses_actual_route_names():
    doc = _read_repo_text("docs/Agent版本治理与Diff对比重构方案.md")

    assert "/api/agent-repositories/main" not in doc
    assert "/api/agent-releases/main" not in doc
    assert "浏览器中可在批次页发布回归通过的候选版本，并在版本页切换到任意已发布版本" in doc
    assert "审批、拒绝和候选回归不作为默认用户入口" in doc

    current_routes = [
        "GET  /api/agent-repository",
        "GET  /api/agent-repository/current",
        "GET  /api/agent-releases",
        "POST /api/agent-releases/{release_id}/restore",
        "GET  /api/agent-change-sets?status=&optimization_task_id=&limit=",
        "POST /api/agent-change-sets/{change_set_id}/publish",
    ]
    for route in current_routes:
        assert route in doc


def test_feedback_current_baseline_uses_governor_and_current_workbench_boundary():
    doc = _read_repo_text("docs/反馈闭环当前实现基线.md")
    readme = _read_repo_text("README.md")

    outdated_phrases = [
        "attribution-analyzer-workspace",
        "proposal-generator-workspace",
        "execution-optimizer-workspace",
        "eval-case-governor-workspace",
        "regression-impact-analyzer-workspace",
        "proposal 审批页面",
        "是否审批 -> 修改了哪个版本",
    ]
    for phrase in outdated_phrases:
        assert phrase not in doc

    current_phrases = [
        "归因、方案、执行、用例治理和回归影响分析职责在运行态由 `governor` 按 job type 承担",
        "`main-agent` 是当前默认业务 Agent 样板；治理职责由 `governor` 承担",
        "反馈信息：查看反馈来源、补充标注、生成或编辑评估用例、加入优化批次",
        "优化批次：聚合多条反馈、运行归因、生成优化方案、执行任务和回归验证",
        "回归资产：管理反馈衍生 eval case、晋级长期资产、标记 flaky、归档或 supersede",
        "版本治理：查看 change set、diff、发布、恢复和版本事件",
    ]
    for phrase in current_phrases:
        assert phrase in doc

    assert "governor-workspace/" in readme


def test_feedback_asset_design_keeps_test_dataset_as_active_authority():
    baseline = _read_repo_text("docs/反馈闭环当前实现基线.md")
    asset_design = _read_repo_text("docs/反馈闭环长期回归资产升级方案.md")

    outdated_phrases = [
        "长期回归资产升级规划新增 Workspace",
        "长期回归资产升级规划新增 Claude root",
        "新增两个规划智能体对应的 profile",
    ]
    for phrase in outdated_phrases:
        assert phrase not in baseline

    current_phrases = [
        "`docs/反馈闭环长期回归资产升级方案.md` 只继续承载测试数据集、回归资产、发布门禁和资产 Registry 的长期设计",
        "`TestDataset` 是测试发布阶段的可版本化测试集合，不等同于一次回归运行",
        "`RegressionAsset` 是从高价值反馈、人工评估或外部事件沉淀出的长期防退化资产",
        "`RegressionRun` 是一次执行记录，绑定候选版本、测试数据集、执行环境、结果快照和失败分析",
        "`ReleaseGate` 是测试发布阶段的风险判断结果",
        "旧“回归资产”独立主菜单、旧“版本管理”独立发布入口和旧批次回归区只作为迁移来源",
    ]
    for phrase in current_phrases[:1]:
        assert phrase in baseline
    for phrase in current_phrases[1:]:
        assert phrase in asset_design
