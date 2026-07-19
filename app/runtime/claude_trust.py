from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class ClaudeWorkspaceProfile(Protocol):
    @property
    def workspace_dir(self) -> Path: ...

    @property
    def claude_config_dir(self) -> Path: ...

    @property
    def trust_workspace_dirs(self) -> tuple[Path, ...]: ...


def ensure_claude_workspace_trusted(profile: ClaudeWorkspaceProfile) -> None:
    """Accept Claude Code's native workspace trust gate for backend-owned runtime profiles."""
    state_path = profile.claude_config_dir / ".claude.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {}

    projects = state.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        state["projects"] = projects

    changed = False
    workspace_dirs = (profile.workspace_dir, *profile.trust_workspace_dirs)
    for workspace_dir in dict.fromkeys(workspace_dirs):
        workspace_key = workspace_dir.as_posix()
        project_state = projects.get(workspace_key)
        if not isinstance(project_state, dict):
            project_state = {}
            projects[workspace_key] = project_state
        if project_state.get("hasTrustDialogAccepted") is True:
            continue
        project_state["hasTrustDialogAccepted"] = True
        changed = True

    if not changed:
        return
    tmp_path = state_path.with_name(f"{state_path.name}.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(state_path)
