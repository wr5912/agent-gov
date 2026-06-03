from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .settings import AppSettings


AgentRole = Literal[
    "main-agent",
    "attribution-analyzer",
    "proposal-generator",
    "execution-optimizer",
    "eval-case-governor",
    "regression-impact-analyzer",
]

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
    allowed_mcp_servers: tuple[str, ...]
    permission_mode: str = "default"
    allowed_tools: tuple[str, ...] = ("Read", "Grep", "Glob")
    disallowed_tools: tuple[str, ...] = ("Bash", "Edit", "Write", "WebFetch", "WebSearch")
    max_turns: int | None = None
    max_runtime_seconds: int = 300
    max_output_bytes: int = 2_000_000


def build_profiles(settings: AppSettings) -> dict[str, AgentRuntimeProfile]:
    return {
        MAIN_AGENT_PROFILE: _main_profile(settings),
        ATTRIBUTION_ANALYZER_PROFILE: _attribution_profile(settings),
        PROPOSAL_GENERATOR_PROFILE: _proposal_profile(settings),
        EXECUTION_OPTIMIZER_PROFILE: _execution_profile(settings),
        EVAL_CASE_GOVERNOR_PROFILE: _eval_case_governor_profile(settings),
        REGRESSION_IMPACT_ANALYZER_PROFILE: _regression_impact_profile(settings),
    }


def _main_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return AgentRuntimeProfile(
        name=MAIN_AGENT_PROFILE,
        role=MAIN_AGENT_PROFILE,
        workspace_dir=settings.main_workspace_dir,
        claude_root=settings.main_claude_root,
        claude_config_dir=settings.main_claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=settings.claude_mcp_config_path or settings.main_workspace_dir / ".mcp.json",
        project_settings_path=settings.main_workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name="runtime.main_agent",
        readable_paths=(settings.main_workspace_dir, settings.data_dir),
        writable_paths=(settings.data_dir,),
        denied_paths=(
            settings.attribution_analyzer_claude_root,
            settings.proposal_generator_claude_root,
            settings.execution_optimizer_claude_root,
            settings.eval_case_governor_claude_root,
            settings.regression_impact_analyzer_claude_root,
        ),
        allowed_mcp_servers=("sec-ops-data", "security-kb"),
        permission_mode=settings.permission_mode or "default",
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
        readable_paths=(settings.data_dir,),
        writable_paths=(settings.data_dir / ".runtime-tmp" / "jobs",),
        denied_paths=(settings.main_workspace_dir, settings.main_claude_root),
        allowed_mcp_servers=("feedback-evidence", "readonly-trace"),
    )


def _proposal_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return _readonly_feedback_profile(
        name=PROPOSAL_GENERATOR_PROFILE,
        workspace_dir=settings.proposal_generator_workspace_dir,
        claude_root=settings.proposal_generator_claude_root,
        observation="runtime.proposal_generator",
        allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
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
            allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
            readable_paths=(settings.data_dir, settings.main_workspace_dir),
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
        allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
        max_turns=10,
        settings=settings,
    )


def _regression_impact_profile(settings: AppSettings) -> AgentRuntimeProfile:
    return _readonly_feedback_profile(
        name=REGRESSION_IMPACT_ANALYZER_PROFILE,
        workspace_dir=settings.regression_impact_analyzer_workspace_dir,
        claude_root=settings.regression_impact_analyzer_claude_root,
        observation="runtime.regression_impact_analyzer",
        allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
        max_turns=10,
        settings=settings,
    )


def _readonly_feedback_profile(
    *,
    name: AgentRole,
    workspace_dir: Path,
    claude_root: Path,
    observation: str,
    allowed_mcp_servers: tuple[str, ...],
    max_turns: int | None,
    settings: AppSettings,
) -> AgentRuntimeProfile:
    return AgentRuntimeProfile(
        **_readonly_feedback_kwargs(
            name=name,
            workspace_dir=workspace_dir,
            claude_root=claude_root,
            observation=observation,
            allowed_mcp_servers=allowed_mcp_servers,
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
    allowed_mcp_servers: tuple[str, ...],
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
        "readable_paths": readable_paths or (settings.data_dir,),
        "writable_paths": (settings.data_dir / ".runtime-tmp" / "jobs",),
        "denied_paths": denied_paths or (settings.main_workspace_dir, settings.main_claude_root),
        "allowed_mcp_servers": allowed_mcp_servers,
        "allowed_tools": (),
        "disallowed_tools": ("Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch"),
        "max_turns": max_turns,
    }
