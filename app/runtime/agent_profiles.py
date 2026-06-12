from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .mcp_config import resolve_main_mcp_config_path
from .settings import AppSettings


AgentRole = Literal[
    "main-agent",
    "business-agent",
    "attribution-analyzer",
    "proposal-generator",
    "execution-optimizer",
    "eval-case-governor",
    "regression-impact-analyzer",
]

# 动态注册业务 Agent 的通用角色（main-agent 是内置首个业务 Agent）。
BUSINESS_AGENT_ROLE = "business-agent"

# 业务 Agent 是被治理对象，治理 Agent 是闭环执行者（AGV-005）。
AgentCategory = Literal["business", "governance"]

# 治理 Agent 角色的单一真相来源；其余角色（当前为 main-agent）归为业务 Agent。
GOVERNANCE_AGENT_ROLES: frozenset[AgentRole] = frozenset(
    {
        "attribution-analyzer",
        "proposal-generator",
        "execution-optimizer",
        "eval-case-governor",
        "regression-impact-analyzer",
    }
)


def agent_category(role: AgentRole) -> AgentCategory:
    """把 Agent 角色映射为业务/治理分类（AGV-005 结构化身份边界）。"""
    return "governance" if role in GOVERNANCE_AGENT_ROLES else "business"

MAIN_AGENT_PROFILE = "main-agent"
ATTRIBUTION_ANALYZER_PROFILE = "attribution-analyzer"
PROPOSAL_GENERATOR_PROFILE = "proposal-generator"
EXECUTION_OPTIMIZER_PROFILE = "execution-optimizer"
EVAL_CASE_GOVERNOR_PROFILE = "eval-case-governor"
REGRESSION_IMPACT_ANALYZER_PROFILE = "regression-impact-analyzer"

PROFILE_VERSION_IDS: dict[AgentRole, str] = {
    "attribution-analyzer": "attribution-analyzer-v0.1.0",
    "proposal-generator": "proposal-generator-v0.1.0",
    "execution-optimizer": "execution-optimizer-v0.1.0",
    "eval-case-governor": "eval-case-governor-v0.1.0",
    "regression-impact-analyzer": "regression-impact-analyzer-v0.1.0",
}


@dataclass(frozen=True)
class AgentRuntimeProfile:
    name: str
    role: AgentRole
    workspace_dir: Path
    claude_root: Path
    claude_config_dir: Path
    data_dir: Path
    mcp_config_path: Path
    project_settings_path: Path
    langfuse_observation_name: str
    readable_paths: tuple[Path, ...]
    writable_paths: tuple[Path, ...]
    denied_paths: tuple[Path, ...]
    max_turns: int | None = None
    max_runtime_seconds: int = 300
    max_output_bytes: int = 2_000_000

    @property
    def category(self) -> AgentCategory:
        """业务 Agent（被治理对象）或治理 Agent（闭环执行者），由角色派生。"""
        return agent_category(self.role)


def build_profiles(settings: AppSettings) -> dict[str, AgentRuntimeProfile]:
    return {
        MAIN_AGENT_PROFILE: _main_profile(settings),
        ATTRIBUTION_ANALYZER_PROFILE: _attribution_profile(settings),
        PROPOSAL_GENERATOR_PROFILE: _proposal_profile(settings),
        EXECUTION_OPTIMIZER_PROFILE: _execution_profile(settings),
        EVAL_CASE_GOVERNOR_PROFILE: _eval_case_governor_profile(settings),
        REGRESSION_IMPACT_ANALYZER_PROFILE: _regression_impact_profile(settings),
    }


def candidate_main_profile(settings: AppSettings, *, workspace_dir: Path, candidate_id: str) -> AgentRuntimeProfile:
    profile = _main_profile(settings)
    claude_root = settings.data_dir / "agent-governance" / "candidate-claude-roots" / candidate_id
    return AgentRuntimeProfile(
        name=f"{MAIN_AGENT_PROFILE}-candidate",
        role=MAIN_AGENT_PROFILE,
        workspace_dir=workspace_dir,
        claude_root=claude_root,
        claude_config_dir=claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=workspace_dir / ".mcp.json",
        project_settings_path=workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name="runtime.main_agent_candidate",
        readable_paths=(workspace_dir, settings.data_dir),
        writable_paths=profile.writable_paths,
        denied_paths=profile.denied_paths,
        max_turns=profile.max_turns,
        max_runtime_seconds=profile.max_runtime_seconds,
        max_output_bytes=profile.max_output_bytes,
    )


def build_business_agent_profile(settings: AppSettings, *, agent_id: str, workspace_dir: Path) -> AgentRuntimeProfile:
    """为一个注册业务 Agent 动态构造运行时 profile（AGV-004 运行态）。

    业务 Agent 是被治理对象：可读自身 workspace 与 data_dir、可写输出目录，
    但不得写入任何治理 Agent 根目录。role 统一为 business-agent，name 为 agent_id。
    """
    claude_root = settings.data_dir / "business-agents" / agent_id / "claude-root"
    return AgentRuntimeProfile(
        name=agent_id,
        role=BUSINESS_AGENT_ROLE,
        workspace_dir=workspace_dir,
        claude_root=claude_root,
        claude_config_dir=claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=workspace_dir / ".mcp.json",
        project_settings_path=workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name=f"runtime.business_agent.{agent_id}",
        readable_paths=(workspace_dir, settings.data_dir),
        writable_paths=(settings.data_dir / "outputs",),
        denied_paths=(
            settings.attribution_analyzer_claude_root,
            settings.proposal_generator_claude_root,
            settings.execution_optimizer_claude_root,
            settings.eval_case_governor_claude_root,
            settings.regression_impact_analyzer_claude_root,
        ),
    )


def _main_profile(settings: AppSettings) -> AgentRuntimeProfile:
    mcp_resolution = resolve_main_mcp_config_path(settings.main_workspace_dir)
    return AgentRuntimeProfile(
        name=MAIN_AGENT_PROFILE,
        role=MAIN_AGENT_PROFILE,
        workspace_dir=settings.main_workspace_dir,
        claude_root=settings.main_claude_root,
        claude_config_dir=settings.main_claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=mcp_resolution.path,
        project_settings_path=settings.main_workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name="runtime.main_agent",
        readable_paths=(settings.main_workspace_dir, settings.data_dir),
        writable_paths=(settings.data_dir / "outputs",),
        denied_paths=(
            settings.attribution_analyzer_claude_root,
            settings.proposal_generator_claude_root,
            settings.execution_optimizer_claude_root,
            settings.eval_case_governor_claude_root,
            settings.regression_impact_analyzer_claude_root,
        ),
    )


def _attribution_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return AgentRuntimeProfile(
        name=ATTRIBUTION_ANALYZER_PROFILE,
        role=ATTRIBUTION_ANALYZER_PROFILE,
        workspace_dir=settings.attribution_analyzer_workspace_dir,
        claude_root=settings.attribution_analyzer_claude_root,
        claude_config_dir=settings.attribution_analyzer_claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=settings.attribution_analyzer_workspace_dir / ".mcp.json",
        project_settings_path=settings.attribution_analyzer_workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name="runtime.attribution_analyzer",
        readable_paths=(),
        writable_paths=(),
        denied_paths=(settings.main_workspace_dir, settings.main_claude_root),
        max_turns=16,
    )


def _proposal_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return _readonly_feedback_profile(
        name=PROPOSAL_GENERATOR_PROFILE,
        workspace_dir=settings.proposal_generator_workspace_dir,
        claude_root=settings.proposal_generator_claude_root,
        observation="runtime.proposal_generator",
        max_turns=16,
        settings=settings,
    )


def _execution_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return AgentRuntimeProfile(
        **_readonly_feedback_kwargs(
            name=EXECUTION_OPTIMIZER_PROFILE,
            workspace_dir=settings.execution_optimizer_workspace_dir,
            claude_root=settings.execution_optimizer_claude_root,
            observation="runtime.execution_optimizer_agent",
            readable_paths=(),
            denied_paths=(settings.main_claude_root,),
            max_turns=12,
            settings=settings,
        )
    )


def _eval_case_governor_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return _readonly_feedback_profile(
        name=EVAL_CASE_GOVERNOR_PROFILE,
        workspace_dir=settings.eval_case_governor_workspace_dir,
        claude_root=settings.eval_case_governor_claude_root,
        observation="runtime.eval_case_governor",
        max_turns=10,
        settings=settings,
    )


def _regression_impact_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return _readonly_feedback_profile(
        name=REGRESSION_IMPACT_ANALYZER_PROFILE,
        workspace_dir=settings.regression_impact_analyzer_workspace_dir,
        claude_root=settings.regression_impact_analyzer_claude_root,
        observation="runtime.regression_impact_analyzer",
        max_turns=10,
        settings=settings,
    )


def _readonly_feedback_profile(
    *,
    name: AgentRole,
    workspace_dir: Path,
    claude_root: Path,
    observation: str,
    max_turns: int | None,
    settings: AppSettings,
) -> AgentRuntimeProfile:
    return AgentRuntimeProfile(
        **_readonly_feedback_kwargs(
            name=name,
            workspace_dir=workspace_dir,
            claude_root=claude_root,
            observation=observation,
            max_turns=max_turns,
            settings=settings,
        )
    )


def _readonly_feedback_kwargs(
    *,
    name: AgentRole,
    workspace_dir: Path,
    claude_root: Path,
    observation: str,
    max_turns: int | None,
    settings: AppSettings,
    readable_paths: tuple[Path, ...] | None = None,
    denied_paths: tuple[Path, ...] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "role": name,
        "workspace_dir": workspace_dir,
        "claude_root": claude_root,
        "claude_config_dir": claude_root / ".claude",
        "data_dir": settings.data_dir,
        "mcp_config_path": workspace_dir / ".mcp.json",
        "project_settings_path": workspace_dir / ".claude" / "settings.json",
        "langfuse_observation_name": observation,
        "readable_paths": readable_paths or (),
        "writable_paths": (),
        "denied_paths": denied_paths or (settings.main_workspace_dir, settings.main_claude_root),
        "max_turns": max_turns,
    }
