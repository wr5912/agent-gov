from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .settings import AppSettings


AgentRole = Literal["main-agent", "attribution-analyzer", "proposal-generator", "execution-optimizer"]

MAIN_AGENT_PROFILE = "main-agent"
ATTRIBUTION_ANALYZER_PROFILE = "attribution-analyzer"
PROPOSAL_GENERATOR_PROFILE = "proposal-generator"
EXECUTION_OPTIMIZER_PROFILE = "execution-optimizer"

PROFILE_VERSION_IDS: dict[AgentRole, str] = {
    "attribution-analyzer": "attribution-analyzer-v0.1.0",
    "proposal-generator": "proposal-generator-v0.1.0",
    "execution-optimizer": "execution-optimizer-v0.1.0",
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
    data_dir = settings.data_dir
    return {
        MAIN_AGENT_PROFILE: AgentRuntimeProfile(
            name=MAIN_AGENT_PROFILE,
            role=MAIN_AGENT_PROFILE,
            workspace_dir=settings.main_workspace_dir,
            claude_root=settings.main_claude_root,
            claude_config_dir=settings.main_claude_root / ".claude",
            data_dir=data_dir,
            mcp_config_path=settings.main_workspace_dir / ".mcp.json",
            project_settings_path=settings.main_workspace_dir / ".claude" / "settings.json",
            langfuse_observation_name="runtime.main_agent",
            readable_paths=(settings.main_workspace_dir, data_dir),
            writable_paths=(data_dir,),
            denied_paths=(
                settings.attribution_analyzer_claude_root,
                settings.proposal_generator_claude_root,
                settings.execution_optimizer_claude_root,
            ),
            allowed_mcp_servers=("sec-ops-data", "security-kb"),
            permission_mode=settings.permission_mode or "default",
        ),
        ATTRIBUTION_ANALYZER_PROFILE: AgentRuntimeProfile(
            name=ATTRIBUTION_ANALYZER_PROFILE,
            role=ATTRIBUTION_ANALYZER_PROFILE,
            workspace_dir=settings.attribution_analyzer_workspace_dir,
            claude_root=settings.attribution_analyzer_claude_root,
            claude_config_dir=settings.attribution_analyzer_claude_root / ".claude",
            data_dir=data_dir,
            mcp_config_path=settings.attribution_analyzer_workspace_dir / ".mcp.json",
            project_settings_path=settings.attribution_analyzer_workspace_dir / ".claude" / "settings.json",
            langfuse_observation_name="runtime.attribution_analyzer",
            readable_paths=(data_dir,),
            writable_paths=(data_dir / ".runtime-tmp" / "jobs",),
            denied_paths=(settings.main_workspace_dir, settings.main_claude_root),
            allowed_mcp_servers=("feedback-evidence", "readonly-trace"),
        ),
        PROPOSAL_GENERATOR_PROFILE: AgentRuntimeProfile(
            name=PROPOSAL_GENERATOR_PROFILE,
            role=PROPOSAL_GENERATOR_PROFILE,
            workspace_dir=settings.proposal_generator_workspace_dir,
            claude_root=settings.proposal_generator_claude_root,
            claude_config_dir=settings.proposal_generator_claude_root / ".claude",
            data_dir=data_dir,
            mcp_config_path=settings.proposal_generator_workspace_dir / ".mcp.json",
            project_settings_path=settings.proposal_generator_workspace_dir / ".claude" / "settings.json",
            langfuse_observation_name="runtime.proposal_generator",
            readable_paths=(data_dir,),
            writable_paths=(data_dir / ".runtime-tmp" / "jobs",),
            denied_paths=(settings.main_workspace_dir, settings.main_claude_root),
            allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
            allowed_tools=(),
            disallowed_tools=("Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch"),
            max_turns=16,
        ),
        EXECUTION_OPTIMIZER_PROFILE: AgentRuntimeProfile(
            name=EXECUTION_OPTIMIZER_PROFILE,
            role=EXECUTION_OPTIMIZER_PROFILE,
            workspace_dir=settings.execution_optimizer_workspace_dir,
            claude_root=settings.execution_optimizer_claude_root,
            claude_config_dir=settings.execution_optimizer_claude_root / ".claude",
            data_dir=data_dir,
            mcp_config_path=settings.execution_optimizer_workspace_dir / ".mcp.json",
            project_settings_path=settings.execution_optimizer_workspace_dir / ".claude" / "settings.json",
            langfuse_observation_name="runtime.execution_optimizer_agent",
            readable_paths=(data_dir, settings.main_workspace_dir),
            writable_paths=(data_dir / ".runtime-tmp" / "jobs",),
            denied_paths=(settings.main_claude_root,),
            allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
            allowed_tools=(),
            disallowed_tools=("Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch"),
            max_turns=12,
        ),
    }
