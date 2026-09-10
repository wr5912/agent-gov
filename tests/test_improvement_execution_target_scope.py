from app.services.improvement_execution_service import _scoped_execution_recommendations


def test_execution_recommendations_only_reference_current_editable_targets() -> None:
    targets = [
        "AGENT.md",
        "agent.yaml",
        "mcp/sec-ops.json",
        "skills/alert-triage/SKILL.md",
    ]
    changes = [
        {"target": "AGENT.md -> 分析流程", "change": "补充时间窗口核验"},
        {"target": "rules/evidence-first.md", "change": "修改规则"},
        {"target": "skills/alert-triage/SKILL.md", "change": "补充回归步骤"},
        {"target": "subagents/soc-analyst/AGENT.md", "change": "修改子 Agent"},
    ]

    assert _scoped_execution_recommendations(changes, targets) == [
        ("AGENT.md", "补充时间窗口核验"),
        ("skills/alert-triage/SKILL.md", "补充回归步骤"),
    ]
