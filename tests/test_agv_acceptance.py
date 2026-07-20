"""AGV 核心功能测试用例的自动验收锚点。

每个测试对应 docs/AgentGov核心功能测试用例.md 中一个 `current` 用例，把可从仓库
事实判定的成功标准固化为可重复回归，支撑「目标达成分阶段执行计划」的阶段 0 固本。
新增对应用例的自动验收时在此追加，并在用例文档登记绑定。
"""

from __future__ import annotations

from pathlib import Path

from test_api_execution_optimizer import _load_app

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_agv_001_governance_platform_positioning() -> None:
    """AGV-001 通用治理平台定位不被单行业绑定：主口径为智能体治理平台 AgentGov。"""
    readme = _read("README.md")
    vision = _read("docs/项目目标愿景使命.md")
    index_html = _read("frontend/index.html")

    assert readme.splitlines()[0].strip() == "# 智能体治理平台 AgentGov"
    assert vision.splitlines()[0].strip() == "# 智能体治理平台 AgentGov 目标、愿景与使命"
    assert "<title>智能体治理平台 AgentGov</title>" in index_html
    # 旧定位已退场；「开发平台」不得作为首屏主定位。
    assert "网络安全运营智能体底座" not in vision
    assert "开发平台" not in readme.splitlines()[0]


def test_agv_003_048_frontend_is_debug_observation_boundary() -> None:
    """AGV-003 / AGV-048 前端边界：调试与治理观察界面，不接管 CLI、不操作生产。"""
    readme = _read("README.md")
    chat = _read("frontend/src/components/ChatPanel.tsx")

    assert "不接管 Claude Code CLI 进程" in readme
    assert "不提供 Terminal" in readme
    assert "通过后端 Runtime API 完成" in readme
    assert "不接管 Claude Code 进程" in chat


def test_agv_046_security_ops_is_replaceable_example_scenario() -> None:
    """AGV-046 安全运营作为示例场景可被替换：平台不绑定单一行业。"""
    vision = _read("docs/项目目标愿景使命.md")
    scene = vision.split("## 典型落地场景", 1)[1].split("## 产品边界", 1)[0]

    for scenario in ("安全运营", "客服", "研发助手", "知识管理", "企业流程自动化"):
        assert scenario in scene
    assert "不定义 AgentGov 的全部产品边界" in scene


def test_agv_018_business_agents_do_not_special_case_historical_main_id() -> None:
    """AGV-018：历史 main ID 不再拥有默认、内置、受保护或模板语义。"""
    vision = _read("docs/项目目标愿景使命.md")
    cases = _read("docs/AgentGov核心功能测试用例.md")
    terms = _read("docs/AgentGov术语与版本边界.md")

    assert "所有注册业务 Agent（含 `main-agent`）" in vision
    assert "`security-operations-expert` 是唯一内置、默认且受保护" in vision
    assert "`main-agent` 只是普通历史示例" in cases
    assert "不再是默认、内置、受保护、模板或隐式兜底" in terms


def test_agv_037_047_governance_scope_not_business_ownership(monkeypatch, tmp_path: Path) -> None:
    """AGV-037/047：AgentGov 只暴露治理端点，不复制外部业务系统信息架构与生产责任。"""
    module = _load_app(monkeypatch, tmp_path)
    paths = {r.path for r in module.app.routes if getattr(r, "path", "").startswith(("/api", "/v1"))}

    # 不接管用户/角色/租户/权限/生产处置等外部业务系统所有权（不复制信息架构）。
    forbidden = {
        "user",
        "users",
        "role",
        "roles",
        "tenant",
        "tenants",
        "permission",
        "permissions",
        "account",
        "accounts",
        "workorder",
        "workorders",
        "ticket",
        "tickets",
        "deploy",
        "deployments",
        "production",
    }
    offending = sorted(p for p in paths if len(p.split("/")) > 2 and p.split("/")[2].lower().strip("{}") in forbidden)
    assert offending == [], f"AgentGov 不应暴露业务系统所有权端点: {offending}"

    # AgentGov 确实拥有治理面：被治理对象、审批门、审计事件、运行记录可追踪。
    assert "/api/agent-registry" in paths
    assert "/api/agent-change-sets/{change_set_id}/approve" in paths  # 高风险审批入口（AGV-041 背书）
    assert "/api/agent-change-sets/{change_set_id}/events" in paths  # 审计事件可追踪
    assert "/api/agent-runs" in paths  # 运行记录可被外部系统追踪

    # 产品边界文档明确职责划分：AgentGov 负责治理，外部系统负责业务/权限/生产。
    boundary = _read("docs/项目目标愿景使命.md").split("## 产品边界", 1)[1]
    assert "AgentGov 负责" in boundary
    assert "外部业务系统负责" in boundary
    assert "高风险动作的人工确认" in boundary


def test_agv_049_collaboration_platform_selection_is_deferred() -> None:
    """AGV-049 当前不接入协作平台，智能体协作留到核心能力稳定后。"""
    vision = _read("docs/项目目标愿景使命.md")
    readme = _read("README.md")

    # 当前产品不预埋通用协作领域模型。
    assert "不提供通用协作看板" in vision
    assert "通用协作看板、issue/task 生命周期、协作成员管理、squad 管理" in vision  # 明列为不属于能力边界
    assert "本期不建设产品内的通用协作模型" in readme

    # 当前不接入外部研发协作平台。
    assert "当前不接入 Multica 或其他外部研发协作平台" in vision
    assert "也不接入外部研发协作平台" in readme
    assert "不定义智能体之间如何协作" in vision

    # 后期多智能体协作重新选型，不预设候选产品或任务分配模型。
    assert "不属于当前阶段目标" in vision
    assert "不预设任何候选产品" in vision
    assert "重新做协作平台选型" in vision
    assert "不预先定义任务分配" in vision
    assert "至少三个产品大版本" not in vision
    assert "当前重点是把智能体开发与反馈优化闭环做强" in vision
