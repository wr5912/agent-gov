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

    # 实际模板根是 docker/runtime-template/，五个治理 workspace 已合并为单一 governor（Issue #3）。
    assert "runtime-template/" in structure
    template_root = REPO_ROOT / "docker" / "runtime-template"
    assert template_root.is_dir()
    for workspace in (
        "main-workspace",
        "governor-workspace",
    ):
        assert f"{workspace}/" in structure, f"README 结构缺少 {workspace}"
        assert (template_root / workspace).is_dir(), f"模板缺少 {workspace}"

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


def test_feedback_product_test_requirements_match_task_execution_flow():
    doc = _read_repo_text("docs/反馈优化产品调整方案.md")

    outdated_phrases = [
        "POST /api/feedback-optimization-batches/{batch_id}/optimization-plan/approve",
        "POST /api/feedback-optimization-batches/{batch_id}/optimization-plan/reject",
        "优化方案审批和拒绝",
        "优化方案审批按钮状态",
        "审批优化方案后执行优化",
        "绕过优化方案审批直接应用 execution plan",
        "用户只审批一次优化方案",
    ]
    for phrase in outdated_phrases:
        assert phrase not in doc

    current_phrases = [
        "开发人员阅读优化方案并选择具体任务执行",
        "PATCH /api/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}",
        "POST /api/feedback-optimization-batches/{batch_id}/optimization-plan/execute-all",
        "优化方案任务编辑、单任务执行和一键执行",
        "用户只在执行具体任务或一键执行时确认一次",
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


def test_feedback_multi_agent_architecture_uses_current_plan_task_routes():
    doc = _read_repo_text("docs/反馈优化闭环多智能体架构.md")

    outdated_phrases = [
        "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
        "/api/feedback-cases/{id}/proposal-jobs",
        "POST /api/optimization-proposals",
        "POST /api/optimization-proposals/{proposal_id}/tasks",
        "proposal 审批页面",
        "          -> 审批",
        "是否审批 -> 修改了哪个版本",
    ]
    for phrase in outdated_phrases:
        assert phrase not in doc

    current_phrases = [
        "POST /api/feedback-cases/{feedback_case_id}/optimization-plan",
        "PATCH /api/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}",
        "POST /api/feedback-optimization-batches/{batch_id}/optimization-plan/execute-all",
        "POST /api/feedback-optimization-batches/{batch_id}/regression-plan",
        "POST /api/feedback-optimization-batches/{batch_id}/regression-runs/{eval_run_id}/gate-overrides",
        "### 19.4 优化方案详情与任务执行",
        "任务动作：编辑、执行、跳过不可执行项、转外部治理或一键执行",
    ]
    for phrase in current_phrases:
        assert phrase in doc


def test_feedback_panorama_matches_current_profiles_assets_and_version_ui():
    doc = _read_repo_text("docs/反馈闭环机制全景画像.md")

    outdated_phrases = [
        "当前运行时已经实现 4 个固定 profile",
        "长期回归资产升级方案中还规划了 2 个新增 profile",
        "Feedback 工作台当前提供三个主菜单",
        "长期回归资产页尚属于升级目标",
        "长期回归资产升级规划新增 Workspace",
        "长期回归资产升级规划新增 Claude root",
        "新增两个规划智能体对应的 profile",
        "查看当前 main agent version",
        "版本管理支持查看、diff、恢复",
    ]
    for phrase in outdated_phrases:
        assert phrase not in doc

    current_phrases = [
        "当前运行时已经实现 6 个固定 profile",
        "Feedback 工作台当前提供四个主菜单",
        "提供反馈信息、优化批次、回归资产、版本管理的统一操作面",
        "| 用例治理智能体 | `eval-case-governor` | `/eval-case-governor-workspace`",
        "| 回归影响分析智能体 | `regression-impact-analyzer` | `/regression-impact-analyzer-workspace`",
        "回归资产页已作为 `反馈信息 / 优化批次 / 回归资产 / 版本管理` 中的独立侧边菜单页落地",
        "regression plan、gate result、gate override 和回归影响分析 job",
        "查看当前 main Agent Git ref 和 release",
        "支持切换到指定已发布版本",
    ]
    for phrase in current_phrases:
        assert phrase in doc
