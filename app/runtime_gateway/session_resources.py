"""AgentScope Session 管理与安全的运行资源投影。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict, overload

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._router_operations import _call
from .client import AgentScopeRuntimeClient, RuntimeUpstreamError
from .provisioning import RuntimeAgentProvisioner


class RuntimeSessionRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=512)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Session name must not be blank")
        return clean


class RuntimeSessionRenameResponse(BaseModel):
    session_id: str
    name: str


class RuntimeWorkspaceStatusResponse(BaseModel):
    available: bool
    at_workspace_root: bool
    git_repository: bool
    git_dirty: bool


class RuntimeWorkspaceToolResponse(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4096)


class RuntimeWorkspaceMcpResponse(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    is_stateful: bool
    is_healthy: bool
    error: str | None = None
    tools: list[RuntimeWorkspaceToolResponse] = Field(default_factory=list)


class RuntimeWorkspaceSkillResponse(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=4096)


class _WorkspaceParams(TypedDict):
    agent_id: str
    session_id: str


def register_session_resource_routes(
    router: APIRouter,
    *,
    client: AgentScopeRuntimeClient,
    provisioner: RuntimeAgentProvisioner,
) -> None:
    @router.patch(
        "/sessions/{session_id}",
        response_model=RuntimeSessionRenameResponse,
        summary="Rename one version-pinned AgentScope Session",
    )
    async def rename_session(
        session_id: str,
        request_data: RuntimeSessionRenameRequest,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> RuntimeSessionRenameResponse:
        binding = provisioner.require_session(session_id, agent_id)
        upstream = await _call(
            client,
            "PATCH",
            f"/sessions/{session_id}",
            params={"agent_id": binding.runtime_agent_id},
            json={"name": request_data.name},
        )
        return _project_session_rename(upstream.body, session_id=session_id, expected_name=request_data.name)

    @router.get(
        "/sessions/{session_id}/workspace/status",
        response_model=RuntimeWorkspaceStatusResponse,
        summary="Read a safe projection of native Workspace status",
    )
    async def workspace_status(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> RuntimeWorkspaceStatusResponse:
        binding = provisioner.require_session(session_id, agent_id)
        upstream = await _call(
            client,
            "GET",
            "/workspace/status",
            params=_workspace_params(binding.runtime_agent_id, session_id),
        )
        return _project_workspace_status(upstream.body)

    @router.get(
        "/sessions/{session_id}/workspace/mcp",
        response_model=list[RuntimeWorkspaceMcpResponse],
        summary="Connect and list native Workspace MCP status without configuration secrets",
    )
    async def workspace_mcps(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> list[RuntimeWorkspaceMcpResponse]:
        binding = provisioner.require_session(session_id, agent_id)
        upstream = await _call(
            client,
            "GET",
            "/workspace/mcp",
            params=_workspace_params(binding.runtime_agent_id, session_id),
        )
        return _project_workspace_mcps(upstream.body)

    @router.get(
        "/sessions/{session_id}/workspace/skills",
        response_model=list[RuntimeWorkspaceSkillResponse],
        summary="List native skills loaded in one Session Workspace",
    )
    async def workspace_skills(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> list[RuntimeWorkspaceSkillResponse]:
        binding = provisioner.require_session(session_id, agent_id)
        upstream = await _call(
            client,
            "GET",
            "/workspace/skill",
            params=_workspace_params(binding.runtime_agent_id, session_id),
        )
        return _project_workspace_skills(upstream.body)


def _workspace_params(runtime_agent_id: str, session_id: str) -> _WorkspaceParams:
    return {"agent_id": runtime_agent_id, "session_id": session_id}


def _project_session_rename(
    body: object,
    *,
    session_id: str,
    expected_name: str,
) -> RuntimeSessionRenameResponse:
    if not isinstance(body, dict) or body.get("id") != session_id:
        raise _invalid_resource_response("Session rename")
    config = body.get("config")
    name = config.get("name") if isinstance(config, dict) else None
    if name != expected_name:
        raise _invalid_resource_response("Session rename")
    return RuntimeSessionRenameResponse(session_id=session_id, name=expected_name)


def _project_workspace_status(body: Any) -> RuntimeWorkspaceStatusResponse:
    if not isinstance(body, dict):
        raise _invalid_resource_response("Workspace status")
    workdir = body.get("workdir")
    cwd = body.get("cwd")
    git = body.get("git")
    if not isinstance(workdir, str) or not workdir or not isinstance(cwd, str) or not cwd:
        raise _invalid_resource_response("Workspace status")
    if git is not None and not isinstance(git, dict):
        raise _invalid_resource_response("Workspace git status")
    dirty = False
    if isinstance(git, dict):
        counts = (git.get("staged"), git.get("unstaged"), git.get("untracked"), git.get("conflicted"))
        if any(type(value) is not int or value < 0 for value in counts):
            raise _invalid_resource_response("Workspace git status")
        numeric_counts = tuple(int(value) for value in counts)
        dirty = any(value > 0 for value in numeric_counts)
    return RuntimeWorkspaceStatusResponse(
        available=True,
        at_workspace_root=cwd == workdir,
        git_repository=git is not None,
        git_dirty=dirty,
    )


def _project_workspace_mcps(body: Any) -> list[RuntimeWorkspaceMcpResponse]:
    if not isinstance(body, list):
        raise _invalid_resource_response("Workspace MCP list")
    projected: list[RuntimeWorkspaceMcpResponse] = []
    for item in body:
        if not isinstance(item, dict) or type(item.get("is_stateful")) is not bool:
            raise _invalid_resource_response("Workspace MCP entry")
        name = _safe_text(item.get("name"), maximum=256, required=True, kind="Workspace MCP entry")
        tools = item.get("tools", [])
        if not isinstance(tools, list) or len(tools) > 1024:
            raise _invalid_resource_response("Workspace MCP tools")
        projected.append(
            RuntimeWorkspaceMcpResponse(
                name=name,
                is_stateful=item["is_stateful"],
                is_healthy=item.get("is_healthy") is True,
                error="connection_failed" if item.get("error") else None,
                tools=_project_tools(tools),
            )
        )
    return projected


def _project_tools(values: list[object]) -> list[RuntimeWorkspaceToolResponse]:
    tools: list[RuntimeWorkspaceToolResponse] = []
    for item in values:
        if not isinstance(item, dict):
            raise _invalid_resource_response("Workspace MCP tool")
        name = _safe_text(item.get("name"), maximum=256, required=True, kind="Workspace MCP tool")
        description = _safe_text(item.get("description"), maximum=4096, required=False, kind="Workspace MCP tool")
        tools.append(RuntimeWorkspaceToolResponse(name=name, description=description))
    return tools


def _project_workspace_skills(body: Any) -> list[RuntimeWorkspaceSkillResponse]:
    if not isinstance(body, list):
        raise _invalid_resource_response("Workspace skill list")
    projected: list[RuntimeWorkspaceSkillResponse] = []
    for item in body:
        if not isinstance(item, dict):
            raise _invalid_resource_response("Workspace skill entry")
        name = _safe_text(item.get("name"), maximum=256, required=True, kind="Workspace skill entry")
        description = _safe_text(item.get("description"), maximum=4096, required=True, kind="Workspace skill entry")
        projected.append(RuntimeWorkspaceSkillResponse(name=name, description=description))
    return projected


@overload
def _safe_text(value: object, *, maximum: int, required: Literal[True], kind: str) -> str: ...


@overload
def _safe_text(value: object, *, maximum: int, required: Literal[False], kind: str) -> str | None: ...


def _safe_text(value: object, *, maximum: int, required: bool, kind: str) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _invalid_resource_response(kind)
    clean = value.strip()
    if (required and not clean) or len(clean) > maximum:
        raise _invalid_resource_response(kind)
    return clean


def _invalid_resource_response(kind: str) -> RuntimeUpstreamError:
    return RuntimeUpstreamError(502, f'{{"detail":"Runtime returned invalid {kind}"}}'.encode())
