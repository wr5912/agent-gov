"""业务 Agent 与 governor 的结构化身份和 AgentScope Harness 边界。"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from app.runtime.agent_profiles import (
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


def test_agent_roles_are_single_source_of_truth() -> None:
    assert set(get_args(AgentRole)) == {"business-agent", GOVERNOR_PROFILE}


def test_agent_categories_are_derived_from_role() -> None:
    assert agent_category("business-agent") == "business"
    assert agent_category(GOVERNOR_PROFILE) == "governance"


def test_business_agent_profile_contains_only_governed_workspace_identity() -> None:
    settings = _settings()
    workspace = settings.data_dir / "business-agents" / "soc-ops" / "workspace"
    profile = build_business_agent_profile(settings, agent_id="soc-ops", workspace_dir=workspace)

    assert profile.role == "business-agent"
    assert profile.category == "business"
    assert profile.name == "soc-ops"
    assert profile.agent_id == "soc-ops"
    assert profile.workspace_dir == workspace
    assert isinstance(profile.workspace_dir, Path)
    assert not hasattr(profile, "project_settings_path")
    assert not hasattr(profile, "permission_mode")


def test_candidate_profile_names_each_immutable_candidate_uniquely() -> None:
    settings = _settings()
    workspace = settings.data_dir / "business-agents" / "soc-ops" / "version" / "worktrees" / "agc-1"

    profile = candidate_profile(settings, agent_id="soc-ops", workspace_dir=workspace, candidate_id="agc-1")

    assert profile.name == "soc-ops-candidate-agc-1"
    assert profile.agent_id == "soc-ops"
    assert profile.workspace_dir == workspace


def test_build_profiles_exposes_only_governor() -> None:
    profiles = build_profiles(_settings())
    assert LEGACY_MAIN_AGENT_ID not in profiles
    assert set(profiles) == {GOVERNOR_PROFILE}
    assert profiles[GOVERNOR_PROFILE].category == "governance"
