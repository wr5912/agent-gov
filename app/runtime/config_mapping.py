"""AgentScope Harness 与治理/运行态边界的只读映射。"""

from __future__ import annotations

from pathlib import Path

from .agent_paths import business_agent_layout, validate_agent_id
from .protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from .schemas import ConfigMappingItem, ConfigMappingResponse
from .settings import AppSettings

DEFAULT_AGENT_ID = DEFAULT_BUSINESS_AGENT_ID
RUNTIME_CONTRACT = "agentscope-app/2.0.8"


def _host_path(path: Path, settings: AppSettings, *, expose: bool) -> str | None:
    if not expose:
        return None
    try:
        return str(Path(settings.host_data_mount) / path.relative_to(settings.data_dir))
    except ValueError:
        if path == settings.governor_workspace_dir:
            return settings.host_governor_workspace_mount
        return None


def _item(
    settings: AppSettings,
    *,
    kind: str,
    path: Path,
    scope: str,
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
        host_mount=_host_path(path, settings, expose=expose_host_mount),
        exists=path.exists(),
        loaded_by_default=load_semantics in {"runtime_loaded", "runtime_materialized"},
        load_semantics=load_semantics,
        display_group=display_group,
        safe_to_edit=safe_to_edit,
        git_policy=git_policy,
        notes=notes,
    )


def build_config_mapping(
    settings: AppSettings,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    expose_host_mount: bool = False,
) -> ConfigMappingResponse:
    safe_agent_id = validate_agent_id(agent_id)
    layout = business_agent_layout(settings.data_dir, safe_agent_id)
    workspace = layout.workspace
    specs = (
        ("manifest", workspace / "agent.yaml", "runtime_loaded", True),
        ("instructions", workspace / "AGENT.md", "runtime_loaded", True),
        ("skills", workspace / "skills", "runtime_materialized", True),
        ("mcp", workspace / "mcp", "runtime_materialized", True),
        ("subagents", workspace / "subagents", "runtime_materialized", True),
        ("tests", workspace / "tests", "governance_only", True),
    )
    mappings = [
        _item(
            settings,
            kind=kind,
            path=path,
            scope="harness",
            load_semantics=semantics,
            display_group="harness",
            safe_to_edit=editable,
            git_policy="tracked",
            expose_host_mount=expose_host_mount,
        )
        for kind, path, semantics, editable in specs
    ]
    mappings.extend(
        [
            _item(
                settings,
                kind="candidate-worktrees",
                path=layout.version_base / "worktrees",
                scope="governance",
                load_semantics="governance_only",
                display_group="versioning",
                safe_to_edit=False,
                git_policy="ignored",
                expose_host_mount=expose_host_mount,
                notes="受控改进候选；发布前不会重绑已有 Session。",
            ),
            _item(
                settings,
                kind="release-archives",
                path=layout.version_base / "releases",
                scope="governance",
                load_semantics="governance_only",
                display_group="versioning",
                safe_to_edit=False,
                git_policy="ignored",
                expose_host_mount=expose_host_mount,
            ),
        ]
    )
    return ConfigMappingResponse(
        agent_id=safe_agent_id,
        runtime_url=settings.agentscope_runtime_url,
        workspace=str(workspace),
        runtime_contract=RUNTIME_CONTRACT,
        mappings=mappings,
    )
