"""AGV-005 业务 Agent 与治理 Agent 的结构化身份边界。

把此前隐含于命名与权限的区分固化为显式 category 单一真相来源，并断言权限边界：
业务 Agent 是被治理对象，治理 Agent 是闭环执行者。
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_profiles import (
    GOVERNANCE_AGENT_ROLES,
    AgentRole,
    agent_category,
    build_business_agent_profile,
    build_profiles,
)
from app.runtime.settings import AppSettings


def _settings() -> AppSettings:
    return AppSettings()


def test_governance_roles_are_single_source_of_truth() -> None:
    all_roles = set(get_args(AgentRole))
    # 业务角色（内置 main-agent 与动态 business-agent）之外即治理角色。
    assert all_roles - {"main-agent", "business-agent"} == GOVERNANCE_AGENT_ROLES
    assert GOVERNANCE_AGENT_ROLES.isdisjoint({"main-agent", "business-agent"})


def test_main_agent_is_business_others_are_governance() -> None:
    assert agent_category("main-agent") == "business"
    assert agent_category("business-agent") == "business"
    for role in GOVERNANCE_AGENT_ROLES:
        assert agent_category(role) == "governance"


def test_build_business_agent_profile_is_governed_business_object() -> None:
    """AGV-004 运行态：动态业务 Agent profile 可构造，且不可写治理 Agent 根目录。"""
    settings = _settings()
    workspace = settings.data_dir / "business-agents" / "soc-ops"
    profile = build_business_agent_profile(settings, agent_id="soc-ops", workspace_dir=workspace)

    assert profile.role == "business-agent"
    assert profile.category == "business"
    assert profile.name == "soc-ops"
    assert profile.workspace_dir == workspace
    governance_roots = {settings.governor_claude_root}
    assert governance_roots <= set(profile.denied_paths)
    assert isinstance(profile.workspace_dir, Path)


def test_business_agent_cannot_self_read_claude_root() -> None:
    """越权读防护：业务 Agent 不得 Read 自身 SDK 运行态家目录（claude-root）。

    denied_paths 必须显式拦截，policy.py 中 denied 优先于 readable。
    """
    from app.runtime.policy import _path_policy_denial

    settings = _settings()
    workspace = settings.data_dir / "business-agents" / "soc-ops"
    profile = build_business_agent_profile(settings, agent_id="soc-ops", workspace_dir=workspace)

    claude_root = settings.data_dir / "business-agents" / "soc-ops" / "claude-root"
    assert claude_root in set(profile.denied_paths)

    # 经 policy 实际拦截：读自身 claude-root 下任意文件（含凭据态 .claude.json）被拒。
    denial = _path_policy_denial("Read", {"file_path": str(claude_root / ".claude" / ".claude.json")}, profile)
    assert denial is not None and "denied" in denial

    # 对照：读自身 workspace 配置文件不受影响，仍允许。
    assert _path_policy_denial("Read", {"file_path": str(workspace / "CLAUDE.md")}, profile) is None


def test_business_agent_read_scope_excludes_runtime_and_other_agents() -> None:
    from app.runtime.policy import _path_policy_denial

    settings = _settings()
    workspace = business_agent_layout(settings.data_dir, "soc-ops").workspace
    profile = build_business_agent_profile(settings, agent_id="soc-ops", workspace_dir=workspace)

    assert _path_policy_denial("Read", {"file_path": str(workspace / "CLAUDE.md")}, profile) is None
    assert _path_policy_denial("Read", {"file_path": str(settings.data_dir / "uploads" / "input.json")}, profile) is None
    assert _path_policy_denial("Write", {"file_path": str(settings.data_dir / "outputs" / "report.md")}, profile) is None

    own_version = business_agent_layout(settings.data_dir, "soc-ops").version_base
    other_workspace = business_agent_layout(settings.data_dir, "other-agent").workspace
    denied_targets = [
        own_version / "worktrees" / "cs-1" / "CLAUDE.md",
        settings.data_dir / "agent-governance" / "worktrees" / "legacy" / "CLAUDE.md",
        settings.runtime_db_path,
        other_workspace / "CLAUDE.md",
    ]
    for target in denied_targets:
        denial = _path_policy_denial("Read", {"file_path": str(target)}, profile)
        assert denial is not None


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
    governance_roots = {settings.governor_claude_root}
    assert governance_roots <= set(main.denied_paths)

    # 治理 Agent（执行者）不持有可写工作区，输出经后端投影而非直接落地。
    for role in GOVERNANCE_AGENT_ROLES:
        assert profiles[role].writable_paths == ()


def test_governor_has_full_read_of_business_agents_and_no_write() -> None:
    """整改：governor 对所有业务 Agent 有完全读取权限（含 .env/secrets）；自身写被硬阻断（写走受治理 apply）。"""
    from app.runtime.agent_profiles import GOVERNOR_PROFILE, build_profiles
    from app.runtime.policy import _path_policy_denial

    settings = _settings()
    gov = build_profiles(settings)[GOVERNOR_PROFILE]
    assert settings.data_dir in set(gov.readable_paths)  # data_dir 覆盖所有业务 Agent workspace
    assert gov.writable_paths == ()  # 写锁死
    assert gov.denied_paths == ()  # 读无 denied（放开）

    ws = settings.data_dir / "business-agents" / "soc-ops" / "workspace"
    # 读放开：可读业务 Agent 的 .env / CLAUDE.md（原则：读全部内容含密钥）。
    assert _path_policy_denial("Read", {"file_path": str(ws / ".env")}, gov) is None
    assert _path_policy_denial("Read", {"file_path": str(ws / "CLAUDE.md")}, gov) is None
    # 非法写入：自身 Write/Edit 被拒（writable_paths 空 + 接线 hook），写业务配置只能走受治理 apply。
    assert _path_policy_denial("Write", {"file_path": str(ws / "CLAUDE.md")}, gov) is not None
    assert _path_policy_denial("Edit", {"file_path": str(ws / ".claude" / "settings.json")}, gov) is not None


def test_governor_build_options_wires_hooks_and_setting_sources() -> None:
    """整改：build_options 必须传 hooks + setting_sources，否则 profile 权限/settings/CLAUDE.md/skill 声明≠实际。"""
    from types import SimpleNamespace

    from app.runtime.agent_job_runner import AgentJobRunner
    from app.runtime.agent_profiles import GOVERNOR_PROFILE, build_profiles

    settings = _settings()
    profiles = build_profiles(settings)
    runner = AgentJobRunner(
        settings=settings,
        profiles=profiles,
        env_builder=lambda p: {},
        output_formatter=SimpleNamespace(),
        provider_router=SimpleNamespace(claude_env=lambda: {}),
    )
    options = runner.build_options(profiles[GOVERNOR_PROFILE])
    assert list(getattr(options, "setting_sources")) == ["project"]
    assert getattr(options, "hooks")  # PreToolUse 路径钩子已接线，profile 权限才真正生效
