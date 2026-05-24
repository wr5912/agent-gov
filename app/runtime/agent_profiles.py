from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .settings import AppSettings


AgentRole = Literal["main", "feedback-attribution", "feedback-proposal", "execution-optimizer"]


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
        "main": AgentRuntimeProfile(
            name="main",
            role="main",
            workspace_dir=settings.main_workspace_dir,
            claude_root=settings.main_claude_root,
            claude_config_dir=settings.main_claude_root / ".claude",
            data_dir=data_dir,
            mcp_config_path=settings.main_workspace_dir / ".mcp.json",
            project_settings_path=settings.main_workspace_dir / ".claude" / "settings.json",
            langfuse_observation_name="runtime.main_agent",
            readable_paths=(settings.main_workspace_dir, data_dir),
            writable_paths=(data_dir,),
            denied_paths=(settings.attribution_claude_root, settings.proposal_claude_root),
            allowed_mcp_servers=("sec-ops-data", "security-kb"),
            permission_mode=settings.permission_mode or "default",
        ),
        "feedback-attribution": AgentRuntimeProfile(
            name="feedback-attribution",
            role="feedback-attribution",
            workspace_dir=settings.attribution_workspace_dir,
            claude_root=settings.attribution_claude_root,
            claude_config_dir=settings.attribution_claude_root / ".claude",
            data_dir=data_dir,
            mcp_config_path=settings.attribution_workspace_dir / ".mcp.json",
            project_settings_path=settings.attribution_workspace_dir / ".claude" / "settings.json",
            langfuse_observation_name="runtime.feedback_attribution_agent",
            readable_paths=(data_dir,),
            writable_paths=(data_dir / ".runtime-tmp" / "jobs",),
            denied_paths=(settings.main_workspace_dir, settings.main_claude_root),
            allowed_mcp_servers=("feedback-evidence", "readonly-trace"),
        ),
        "feedback-proposal": AgentRuntimeProfile(
            name="feedback-proposal",
            role="feedback-proposal",
            workspace_dir=settings.proposal_workspace_dir,
            claude_root=settings.proposal_claude_root,
            claude_config_dir=settings.proposal_claude_root / ".claude",
            data_dir=data_dir,
            mcp_config_path=settings.proposal_workspace_dir / ".mcp.json",
            project_settings_path=settings.proposal_workspace_dir / ".claude" / "settings.json",
            langfuse_observation_name="runtime.feedback_proposal_agent",
            readable_paths=(data_dir,),
            writable_paths=(data_dir / ".runtime-tmp" / "jobs",),
            denied_paths=(settings.main_workspace_dir, settings.main_claude_root),
            allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
            allowed_tools=(),
            disallowed_tools=("Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch"),
            max_turns=16,
        ),
    }
