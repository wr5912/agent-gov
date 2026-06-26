from __future__ import annotations

from pathlib import Path

from .schemas import ConfigMappingItem, ConfigMappingResponse
from .settings import AppSettings


def _host_path(path: Path, settings: AppSettings) -> str | None:
    pairs = [
        # main 已并入 /data 下（workspace/claude-root 经 data 挂载映射）；governor 仍是顶层挂载。
        (settings.data_dir, settings.host_data_mount),
        (settings.governor_workspace_dir, settings.host_governor_workspace_mount),
        (settings.governor_claude_root, settings.host_governor_claude_root_mount),
    ]
    for container_root, host_root in pairs:
        try:
            rel = path.relative_to(container_root)
        except ValueError:
            continue
        return str(Path(host_root) / rel)
    return None


def _item(
    settings: AppSettings,
    *,
    scope: str,
    kind: str,
    path: Path,
    loaded_by_default: bool,
    git_policy: str,
    notes: str | None = None,
) -> ConfigMappingItem:
    return ConfigMappingItem(
        scope=scope,
        kind=kind,
        container_path=str(path),
        host_mount=_host_path(path, settings),
        exists=path.exists(),
        loaded_by_default=loaded_by_default,
        git_policy=git_policy,
        notes=notes,
    )


def _user_mapping_items(settings: AppSettings, *, loaded_by_default: bool) -> list[ConfigMappingItem]:
    return [
        _item(settings, scope="user", kind="settings", path=settings.claude_home / "settings.json", loaded_by_default=loaded_by_default, git_policy="ignored"),
        _item(settings, scope="user", kind="instructions", path=settings.claude_home / "CLAUDE.md", loaded_by_default=loaded_by_default, git_policy="ignored"),
        _item(settings, scope="user", kind="skills", path=settings.claude_home / "skills", loaded_by_default=loaded_by_default, git_policy="ignored"),
        _item(settings, scope="user", kind="agents", path=settings.claude_home / "agents", loaded_by_default=loaded_by_default, git_policy="ignored"),
        _item(settings, scope="user", kind="commands", path=settings.claude_home / "commands", loaded_by_default=loaded_by_default, git_policy="ignored"),
        _item(settings, scope="user", kind="output-styles", path=settings.claude_home / "output-styles", loaded_by_default=loaded_by_default, git_policy="ignored"),
        _item(
            settings,
            scope="global",
            kind="state",
            path=settings.claude_global_config_file,
            loaded_by_default=True,
            git_policy="ignored",
            notes="Authentication, MCP state, trust state, and caches; do not hand-edit or expose contents.",
        ),
    ]


def _project_mapping_items(
    settings: AppSettings,
    *,
    project_loaded: bool,
    local_loaded: bool,
) -> list[ConfigMappingItem]:
    project = settings.workspace_dir
    return [
        _item(settings, scope="project", kind="instructions", path=project / "CLAUDE.md", loaded_by_default=project_loaded, git_policy="tracked"),
        _item(settings, scope="local", kind="instructions", path=project / "CLAUDE.local.md", loaded_by_default=local_loaded, git_policy="ignored"),
        _item(settings, scope="project", kind="mcp", path=project / ".mcp.json", loaded_by_default=project_loaded, git_policy="tracked"),
        _item(
            settings,
            scope="project",
            kind="worktree-include",
            path=project / ".worktreeinclude",
            loaded_by_default=False,
            git_policy="tracked",
            notes="Used by Claude Code worktree creation to copy selected gitignored files.",
        ),
        _item(settings, scope="project", kind="settings", path=project / ".claude" / "settings.json", loaded_by_default=project_loaded, git_policy="tracked"),
        _item(settings, scope="local", kind="settings", path=project / ".claude" / "settings.local.json", loaded_by_default=local_loaded, git_policy="ignored"),
        _item(settings, scope="project", kind="rules", path=project / ".claude" / "rules", loaded_by_default=project_loaded, git_policy="tracked"),
        _item(settings, scope="project", kind="skills", path=project / ".claude" / "skills", loaded_by_default=project_loaded, git_policy="tracked"),
        _item(settings, scope="project", kind="commands", path=project / ".claude" / "commands", loaded_by_default=project_loaded, git_policy="tracked"),
        _item(settings, scope="project", kind="agents", path=project / ".claude" / "agents", loaded_by_default=project_loaded, git_policy="tracked"),
        _item(settings, scope="project", kind="output-styles", path=project / ".claude" / "output-styles", loaded_by_default=project_loaded, git_policy="tracked"),
    ]


def _runtime_mapping_items(settings: AppSettings) -> list[ConfigMappingItem]:
    items = [
        _item(
            settings,
            scope="runtime",
            kind="agent-git-repository",
            path=settings.agent_git_repository_dir,
            loaded_by_default=True,
            git_policy="tracked",
            notes="Git-backed source of truth for published main Agent configuration.",
        ),
        _item(
            settings,
            scope="runtime",
            kind="agent-change-set-worktrees",
            path=settings.agent_git_worktrees_dir,
            loaded_by_default=True,
            git_policy="ignored",
            notes="Candidate worktrees for reviewed Agent change sets.",
        ),
        _item(
            settings,
            scope="runtime",
            kind="agent-release-archives",
            path=settings.agent_release_archives_dir,
            loaded_by_default=True,
            git_policy="ignored",
            notes="Immutable release archives created during Agent publish.",
        ),
    ]
    if not settings.resolved_claude_config_dir:
        return items
    return [
        *items,
        _item(
            settings,
            scope="runtime",
            kind="redirected-config-dir",
            path=settings.resolved_claude_config_dir,
            loaded_by_default=True,
            git_policy="ignored",
            notes="Only used when CLAUDE_CONFIG_DIR is explicitly set.",
        ),
        _item(
            settings,
            scope="runtime",
            kind="redirected-project-transcripts",
            path=settings.claude_projects_dir,
            loaded_by_default=True,
            git_policy="ignored",
        ),
    ]


def build_config_mapping(settings: AppSettings) -> ConfigMappingResponse:
    sources = settings.setting_sources
    source_set = set(sources or [])
    user_loaded = sources is None or "user" in source_set
    project_loaded = sources is None or "project" in source_set
    local_loaded = sources is None or "local" in source_set
    mappings = [
        *_user_mapping_items(settings, loaded_by_default=user_loaded),
        *_project_mapping_items(settings, project_loaded=project_loaded, local_loaded=local_loaded),
        *_runtime_mapping_items(settings),
    ]

    return ConfigMappingResponse(
        claude_config_mode=settings.claude_config_mode,
        claude_root=str(settings.claude_root),
        claude_home=str(settings.claude_home),
        claude_global_config_file=str(settings.claude_global_config_file),
        claude_config_dir=str(settings.resolved_claude_config_dir) if settings.resolved_claude_config_dir else None,
        setting_sources_effective=sources,
        mappings=mappings,
    )
