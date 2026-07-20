"""AGV-005 业务 Agent 与治理 Agent 的结构化身份和原生项目策略边界。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import get_args

from app.runtime.agent_job_runner import AgentJobRunner
from app.runtime.agent_profiles import (
    GOVERNANCE_AGENT_ROLES,
    GOVERNOR_PROFILE,
    AgentRole,
    agent_category,
    build_business_agent_profile,
    build_profiles,
    candidate_profile,
)
from app.runtime.settings import AppSettings

from business_agent_test_utils import LEGACY_MAIN_AGENT_ID


def _settings() -> AppSettings:
    return AppSettings(_env_file=None)


def test_governance_roles_are_single_source_of_truth() -> None:
    all_roles = set(get_args(AgentRole))
    assert all_roles - {"business-agent"} == GOVERNANCE_AGENT_ROLES
    assert GOVERNANCE_AGENT_ROLES.isdisjoint({"business-agent"})


def test_agent_categories_are_derived_from_role() -> None:
    assert agent_category("business-agent") == "business"
    for role in GOVERNANCE_AGENT_ROLES:
        assert agent_category(role) == "governance"


def test_business_agent_profile_only_selects_native_project_policy() -> None:
    settings = _settings()
    workspace = settings.data_dir / "business-agents" / "soc-ops" / "workspace"
    profile = build_business_agent_profile(settings, agent_id="soc-ops", workspace_dir=workspace)

    assert profile.role == "business-agent"
    assert profile.category == "business"
    assert profile.name == "soc-ops"
    assert profile.agent_id == "soc-ops"
    assert profile.workspace_dir == workspace
    assert profile.project_settings_path == workspace / ".claude" / "settings.json"
    assert isinstance(profile.workspace_dir, Path)
    assert not hasattr(profile, "readable_paths")
    assert not hasattr(profile, "writable_paths")
    assert not hasattr(profile, "denied_paths")


def test_candidate_profile_keeps_runtime_name_separate_from_business_agent_owner() -> None:
    settings = _settings()
    workspace = settings.data_dir / "business-agents" / "soc-ops" / "version" / "worktrees" / "agc-1"

    profile = candidate_profile(settings, agent_id="soc-ops", workspace_dir=workspace, candidate_id="agc-1")

    assert profile.name == "soc-ops-candidate"
    assert profile.agent_id == "soc-ops"
    assert profile.workspace_dir == workspace


def test_build_profiles_exposes_only_governance_profiles() -> None:
    """静态 profile 只有治理执行者；业务 Agent 一律由磁盘发现与注册表提供。"""

    profiles = build_profiles(_settings())
    assert LEGACY_MAIN_AGENT_ID not in profiles
    assert {name for name, profile in profiles.items() if profile.category == "governance"} == GOVERNANCE_AGENT_ROLES
    assert [name for name, profile in profiles.items() if profile.category == "business"] == []


def test_governor_build_options_uses_project_discovery_without_policy_injection() -> None:
    settings = _settings()
    profiles = build_profiles(settings)
    runner = AgentJobRunner(
        settings=settings,
        profiles=profiles,
        env_builder=lambda profile: {},
        output_formatter=SimpleNamespace(),
        provider_router=SimpleNamespace(claude_env=lambda: {}),
    )

    options = runner.build_options(profiles[GOVERNOR_PROFILE])

    assert list(options.setting_sources or []) == ["project"]
    assert options.hooks is None
    assert options.permission_mode is None
    assert options.allowed_tools == []
    assert options.disallowed_tools == []
