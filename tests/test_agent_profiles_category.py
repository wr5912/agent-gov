"""AGV-005 业务 Agent 与治理 Agent 的结构化身份边界。

把此前隐含于命名与权限的区分固化为显式 category 单一真相来源，并断言权限边界：
业务 Agent 是被治理对象，治理 Agent 是闭环执行者。
"""

from __future__ import annotations

from typing import get_args

from app.runtime.agent_profiles import (
    GOVERNANCE_AGENT_ROLES,
    AgentRole,
    agent_category,
    build_profiles,
)
from app.runtime.settings import AppSettings


def _settings() -> AppSettings:
    return AppSettings()


def test_governance_roles_are_single_source_of_truth() -> None:
    all_roles = set(get_args(AgentRole))
    assert all_roles - {"main-agent"} == GOVERNANCE_AGENT_ROLES
    assert "main-agent" not in GOVERNANCE_AGENT_ROLES


def test_main_agent_is_business_others_are_governance() -> None:
    assert agent_category("main-agent") == "business"
    for role in GOVERNANCE_AGENT_ROLES:
        assert agent_category(role) == "governance"


def test_build_profiles_expose_category() -> None:
    profiles = build_profiles(_settings())
    assert profiles["main-agent"].category == "business"
    governance = [name for name, profile in profiles.items() if profile.category == "governance"]
    assert set(governance) == GOVERNANCE_AGENT_ROLES


def test_business_agent_is_governed_object_governance_agents_are_executors() -> None:
    settings = _settings()
    profiles = build_profiles(settings)
    main = profiles["main-agent"]

    # 业务 Agent（被治理对象）不得写入治理 Agent 的根目录。
    governance_roots = {
        settings.attribution_analyzer_claude_root,
        settings.proposal_generator_claude_root,
        settings.execution_optimizer_claude_root,
        settings.eval_case_governor_claude_root,
        settings.regression_impact_analyzer_claude_root,
    }
    assert governance_roots <= set(main.denied_paths)

    # 治理 Agent（执行者）不持有可写工作区，输出经后端投影而非直接落地。
    for role in GOVERNANCE_AGENT_ROLES:
        assert profiles[role].writable_paths == ()
