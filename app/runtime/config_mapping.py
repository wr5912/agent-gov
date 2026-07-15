from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeAlias

from .agent_paths import business_agent_layout, validate_agent_id
from .schemas import ConfigMappingItem, ConfigMappingResponse
from .settings import AppSettings

DEFAULT_AGENT_ID = "main-agent"

_UserItemSpec: TypeAlias = tuple[str, tuple[str, ...]]
_ProjectItemProfile = Literal["project", "local", "worktree"]
_ProjectItemSpec: TypeAlias = tuple[_ProjectItemProfile, str, tuple[str, ...]]

_USER_ITEM_SPECS: tuple[_UserItemSpec, ...] = (
    ("settings", ("settings.json",)),
    ("instructions", ("CLAUDE.md",)),
    ("skills", ("skills",)),
    ("agents", ("agents",)),
    ("commands", ("commands",)),
    ("output-styles", ("output-styles",)),
)

_PROJECT_ITEM_SPECS: tuple[_ProjectItemSpec, ...] = (
    ("project", "instructions", ("CLAUDE.md",)),
    ("local", "instructions", ("CLAUDE.local.md",)),
    ("project", "mcp", (".mcp.json",)),
    ("worktree", "worktree-include", (".worktreeinclude",)),
    ("project", "settings", (".claude", "settings.json")),
    ("local", "settings", (".claude", "settings.local.json")),
    ("project", "rules", (".claude", "rules")),
    ("project", "skills", (".claude", "skills")),
    ("project", "commands", (".claude", "commands")),
    ("project", "agents", (".claude", "agents")),
    ("project", "output-styles", (".claude", "output-styles")),
)


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


def _user_item_from_spec(
    settings: AppSettings,
    *,
    claude_home: Path,
    spec: _UserItemSpec,
    loaded_by_default: bool,
    expose_host_mount: bool,
) -> ConfigMappingItem:
    kind, relative_parts = spec
    return _item(
        settings,
        scope="user",
        kind=kind,
        path=claude_home.joinpath(*relative_parts),
        loaded_by_default=loaded_by_default,
        load_semantics="claude_optional",
        display_group="agent_user_state",
        safe_to_edit=False,
        git_policy="ignored",
        expose_host_mount=expose_host_mount,
    )


def _user_mapping_items(
    settings: AppSettings,
    *,
    claude_root: Path,
    loaded_by_default: bool,
    expose_host_mount: bool,
) -> list[ConfigMappingItem]:
    claude_home = claude_root / ".claude"
    items = [
        _user_item_from_spec(
            settings,
            claude_home=claude_home,
            spec=spec,
            loaded_by_default=loaded_by_default,
            expose_host_mount=expose_host_mount,
        )
        for spec in _USER_ITEM_SPECS
    ]
    return [
        *items,
        _item(
            settings,
            scope="global",
            kind="state",
            path=claude_root / ".claude.json",
            loaded_by_default=True,
            load_semantics="claude_optional",
            display_group="agent_user_state",
            safe_to_edit=False,
            git_policy="ignored",
            expose_host_mount=expose_host_mount,
            notes="Authentication, MCP state, trust state, and caches; do not hand-edit or expose contents.",
        ),
    ]


def _project_item_from_spec(
    settings: AppSettings,
    *,
    project: Path,
    spec: _ProjectItemSpec,
    project_loaded: bool,
    local_loaded: bool,
    expose_host_mount: bool,
) -> ConfigMappingItem:
    profile, kind, relative_parts = spec
    if profile == "local":
        scope = "local"
        loaded_by_default = local_loaded
        load_semantics = "claude_optional"
        display_group = "agent_user_state"
        safe_to_edit = False
        git_policy = "ignored"
    else:
        scope = "project"
        loaded_by_default = project_loaded
        load_semantics = "claude_loaded"
        display_group = "agent_project_config"
        safe_to_edit = True
        git_policy = "tracked"
    notes = None
    if profile == "worktree":
        loaded_by_default = False
        load_semantics = "not_applicable"
        notes = "Used by Claude Code worktree creation to copy selected gitignored files."
    return _item(
        settings,
        scope=scope,
        kind=kind,
        path=project.joinpath(*relative_parts),
        loaded_by_default=loaded_by_default,
        load_semantics=load_semantics,
        display_group=display_group,
        safe_to_edit=safe_to_edit,
        git_policy=git_policy,
        expose_host_mount=expose_host_mount,
        notes=notes,
    )


def _project_mapping_items(
    settings: AppSettings,
    *,
    project: Path,
    project_loaded: bool,
    local_loaded: bool,
    expose_host_mount: bool,
) -> list[ConfigMappingItem]:
    return [
        _project_item_from_spec(
            settings,
            project=project,
            spec=spec,
            project_loaded=project_loaded,
            local_loaded=local_loaded,
            expose_host_mount=expose_host_mount,
        )
        for spec in _PROJECT_ITEM_SPECS
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
    source_set = set(sources)
    user_loaded = "user" in source_set
    project_loaded = "project" in source_set
    local_loaded = "local" in source_set
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
