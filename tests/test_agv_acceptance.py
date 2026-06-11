"""AGV 核心功能测试用例的自动验收锚点。

每个测试对应 docs/AgentGov核心功能测试用例.md 中一个 `current` 用例，把可从仓库
事实判定的成功标准固化为可重复回归，支撑「目标达成分阶段执行计划」的阶段 0 固本。
新增对应用例的自动验收时在此追加，并在用例文档登记绑定。
"""

from __future__ import annotations

from pathlib import Path

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
