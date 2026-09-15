"""AGV 场景说明的文档契约，不作为产品领域中立或真实 Agent 验收。"""

from pathlib import Path


def test_agv_046_documentation_lists_replaceable_example_scenarios() -> None:
    root = Path(__file__).resolve().parents[1]
    vision = (root / "docs/项目目标愿景使命.md").read_text(encoding="utf-8")
    scene = vision.split("## 典型落地场景", 1)[1].split("## 产品边界", 1)[0]

    for scenario in ("安全运营", "客服", "研发助手", "知识管理", "企业流程自动化"):
        assert scenario in scene
    assert "不定义 AgentGov 的全部产品边界" in scene
