from __future__ import annotations

from pathlib import Path

from .agent_paths import business_agent_layout, validate_agent_id
from .schemas import ConfigMappingItem, ConfigMappingResponse
from .settings import AppSettings


DEFAULT_AGENT_ID = "main-agent"


# This module only projects Claude Code/AgentGov paths for the runtime-settings UI.
# The execution truth remains the Claude SDK options and the business-agent workspace files.
def _host_path(path: Path, settings: AppSettings, *, expose_host_mount: bool) -> str | None:
    if not expose_host_mount:
        return None
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
    load_semantics: str,
    display_group: str,
    safe_to_edit: bool,
    git_policy: str,
    expose_host_mount: bool,
    notes: str | None = None,
) -> ConfigMappingItem:
    return ConfigMappingItem(
        scope=scope,
        kind=kind,
        container_path=str(path),
        host_mount=_host_path(path, settings, expose_host_mount=expose_host_mount),
        exists=path.exists(),
        loaded_by_default=loaded_by_default,
        load_semantics=load_semantics,
        display_group=display_group,
        safe_to_edit=safe_to_edit,
        git_policy=git_policy,
        notes=notes,
    )


def _user_mapping_items(
    settings: AppSettings,
    *,
    claude_root: Path,
    loaded_by_default: bool,
    expose_host_mount: bool,
) -> list[ConfigMappingItem]:
    claude_home = claude_root / ".claude"
    global_state = claude_root / ".claude.json"
    return [
        _item(settings, scope="user", kind="settings", path=claude_home / "settings.json", loaded_by_default=loaded_by_default, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(settings, scope="user", kind="instructions", path=claude_home / "CLAUDE.md", loaded_by_default=loaded_by_default, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(settings, scope="user", kind="skills", path=claude_home / "skills", loaded_by_default=loaded_by_default, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(settings, scope="user", kind="agents", path=claude_home / "agents", loaded_by_default=loaded_by_default, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(settings, scope="user", kind="commands", path=claude_home / "commands", loaded_by_default=loaded_by_default, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(settings, scope="user", kind="output-styles", path=claude_home / "output-styles", loaded_by_default=loaded_by_default, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(
            settings,
            scope="global",
            kind="state",
            path=global_state,
            loaded_by_default=True,
            load_semantics="claude_optional",
            display_group="agent_user_state",
            safe_to_edit=False,
            git_policy="ignored",
            expose_host_mount=expose_host_mount,
            notes="Authentication, MCP state, trust state, and caches; do not hand-edit or expose contents.",
        ),
    ]


def _project_mapping_items(
    settings: AppSettings,
    *,
    project: Path,
    project_loaded: bool,
    local_loaded: bool,
    expose_host_mount: bool,
) -> list[ConfigMappingItem]:
    return [
        _item(settings, scope="project", kind="instructions", path=project / "CLAUDE.md", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
        _item(settings, scope="local", kind="instructions", path=project / "CLAUDE.local.md", loaded_by_default=local_loaded, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(settings, scope="project", kind="mcp", path=project / ".mcp.json", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
        _item(
            settings,
            scope="project",
            kind="worktree-include",
            path=project / ".worktreeinclude",
            loaded_by_default=False,
            load_semantics="not_applicable",
            display_group="agent_project_config",
            safe_to_edit=True,
            git_policy="tracked",
            expose_host_mount=expose_host_mount,
            notes="Used by Claude Code worktree creation to copy selected gitignored files.",
        ),
        _item(settings, scope="project", kind="settings", path=project / ".claude" / "settings.json", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
        _item(settings, scope="local", kind="settings", path=project / ".claude" / "settings.local.json", loaded_by_default=local_loaded, load_semantics="claude_optional", display_group="agent_user_state", safe_to_edit=False, git_policy="ignored", expose_host_mount=expose_host_mount),
        _item(settings, scope="project", kind="rules", path=project / ".claude" / "rules", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
        _item(settings, scope="project", kind="skills", path=project / ".claude" / "skills", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
        _item(settings, scope="project", kind="commands", path=project / ".claude" / "commands", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
        _item(settings, scope="project", kind="agents", path=project / ".claude" / "agents", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
        _item(settings, scope="project", kind="output-styles", path=project / ".claude" / "output-styles", loaded_by_default=project_loaded, load_semantics="claude_loaded", display_group="agent_project_config", safe_to_edit=True, git_policy="tracked", expose_host_mount=expose_host_mount),
    ]


def _runtime_mapping_items(
    settings: AppSettings,
    *,
    agent_id: str,
    project: Path,
    version_base: Path,
    expose_host_mount: bool,
) -> list[ConfigMappingItem]:
    items = [
        _item(
            settings,
            scope="runtime",
            kind="agent-git-repository",
            path=project,
            loaded_by_default=False,
            load_semantics="runtime_used",
            display_group="versioning_runtime",
            safe_to_edit=False,
            git_policy="tracked",
            expose_host_mount=expose_host_mount,
            notes=f"Git-backed source of truth for published {agent_id} configuration.",
        ),
        _item(
            settings,
            scope="runtime",
            kind="agent-change-set-worktrees",
            path=version_base / "worktrees",
            loaded_by_default=False,
            load_semantics="runtime_used",
            display_group="versioning_runtime",
            safe_to_edit=False,
            git_policy="ignored",
            expose_host_mount=expose_host_mount,
            notes="Candidate worktrees for reviewed Agent change sets.",
        ),
        _item(
            settings,
            scope="runtime",
            kind="agent-release-archives",
            path=version_base / "releases",
            loaded_by_default=False,
            load_semantics="runtime_used",
            display_group="versioning_runtime",
            safe_to_edit=False,
            git_policy="ignored",
            expose_host_mount=expose_host_mount,
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
            load_semantics="runtime_used",
            display_group="hidden_debug",
            safe_to_edit=False,
            git_policy="ignored",
            expose_host_mount=expose_host_mount,
            notes="Only used when CLAUDE_CONFIG_DIR is explicitly set.",
        ),
        _item(
            settings,
            scope="runtime",
            kind="redirected-project-transcripts",
            path=settings.claude_projects_dir,
            loaded_by_default=True,
            load_semantics="runtime_used",
            display_group="hidden_debug",
            safe_to_edit=False,
            git_policy="ignored",
            expose_host_mount=expose_host_mount,
        ),
    ]


def build_config_mapping(
    settings: AppSettings,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    expose_host_mount: bool = False,
) -> ConfigMappingResponse:
    safe_agent_id = validate_agent_id(agent_id)
    layout = business_agent_layout(settings.data_dir, safe_agent_id)
    sources = settings.setting_sources
    source_set = set(sources or [])
    user_loaded = sources is None or "user" in source_set
    project_loaded = sources is None or "project" in source_set
    local_loaded = sources is None or "local" in source_set
    mappings = [
        *_user_mapping_items(
            settings,
            claude_root=layout.claude_root,
            loaded_by_default=user_loaded,
            expose_host_mount=expose_host_mount,
        ),
        *_project_mapping_items(
            settings,
            project=layout.workspace,
            project_loaded=project_loaded,
            local_loaded=local_loaded,
            expose_host_mount=expose_host_mount,
        ),
        *_runtime_mapping_items(
            settings,
            agent_id=safe_agent_id,
            project=layout.workspace,
            version_base=layout.version_base,
            expose_host_mount=expose_host_mount,
        ),
    ]

    return ConfigMappingResponse(
        agent_id=safe_agent_id,
        claude_config_mode=settings.claude_config_mode,
        claude_root=str(layout.claude_root),
        claude_home=str(layout.claude_root / ".claude"),
        claude_global_config_file=str(layout.claude_root / ".claude.json"),
        claude_config_dir=str(settings.resolved_claude_config_dir) if settings.resolved_claude_config_dir else None,
        setting_sources_effective=sources,
        mappings=mappings,
    )
