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
        "/api/agent-releases/{release_id}/rollback",
        "/data/agent-governance/worktrees/",
        "/data/agent-governance/releases/",
    ]
    for route in current_routes:
        assert route in readme


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
    assert "浏览器中可完成候选 diff 审查、发布和回滚" in doc
    assert "审批、拒绝和候选回归不作为默认用户入口" in doc

    current_routes = [
        "GET  /api/agent-repository",
        "GET  /api/agent-repository/current",
        "GET  /api/agent-releases",
        "POST /api/agent-releases/{release_id}/rollback",
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
        "查看当前 main Agent Git ref、change set 和 release",
        "支持发布和回滚",
    ]
    for phrase in current_phrases:
        assert phrase in doc
