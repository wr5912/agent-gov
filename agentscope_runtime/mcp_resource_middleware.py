"""通过官方 MCP SDK 为 AgentScope 补充受治理的只读 resource 工具。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import httpx
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolBase
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextResourceContents
from pydantic import AnyUrl, TypeAdapter

_TEMPLATE_VARIABLE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_MAX_CURSOR_LENGTH = 2_048
_MAX_URI_LENGTH = 4_096
_MAX_RESULT_BYTES = 1 * 1024 * 1024
_MAX_ROSTER_PAGES = 32
_MAX_ROSTER_ENTRIES = 10_000
_ANY_URL = TypeAdapter(AnyUrl)


@dataclass(frozen=True)
class MCPResourcePolicy:
    """一个版本化 MCP server 可向模型公开的精确 resource 集合。"""

    server_name: str
    resources: frozenset[str]
    resource_templates: tuple[str, ...]

    def allows_uri(self, uri: str) -> bool:
        if uri in self.resources:
            return True
        return any(_template_pattern(template).fullmatch(uri) for template in self.resource_templates)


class MCPResourceMiddleware(MiddlewareBase):
    """把 MCP resources 映射为三个只读 FunctionTool；未知 URI 默认拒绝。"""

    def __init__(
        self,
        clients: list[MCPClient],
        policies: tuple[MCPResourcePolicy, ...],
    ) -> None:
        self._clients = {client.name: client for client in clients}
        self._policies = {policy.server_name: policy for policy in policies}
        self._advertised: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        self._roster_lock = asyncio.Lock()
        if set(self._policies) - set(self._clients):
            raise ValueError("MCP resource policy references an undeclared server")
        for client in self._clients.values():
            if not isinstance(client.mcp_config, HttpMCPConfig):
                raise ValueError("MCP resource facade only supports http_mcp")

    async def list_tools(self) -> list[ToolBase]:
        allow = PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Read-only access to versioned MCP resources",
            decision_reason="agentgov.versioned_mcp_resource",
        )
        tools: list[ToolBase] = []
        for server_name in sorted(self._policies):
            policy = self._policies[server_name]
            if not policy.resources and not policy.resource_templates:
                continue
            await self._require_advertised_policy(server_name)
            tools.extend(
                [
                    FunctionTool(
                        self._list_resources_callable(server_name),
                        name=f"mcp__{server_name}__resources_list",
                        description=(f"列出 {server_name} 当前页中已由 Harness 精确批准的 MCP resources。"),
                        is_read_only=True,
                        permission=allow,
                    ),
                    FunctionTool(
                        self._list_templates_callable(server_name),
                        name=f"mcp__{server_name}__resource_templates_list",
                        description=(f"列出 {server_name} 当前页中已由 Harness 精确批准的 MCP resource templates。"),
                        is_read_only=True,
                        permission=allow,
                    ),
                    FunctionTool(
                        self._read_resource_callable(server_name),
                        name=f"mcp__{server_name}__resource_read",
                        description=(f"读取 {server_name} Harness allowlist 中的一个 MCP resource URI。"),
                        is_read_only=True,
                        permission=allow,
                    ),
                ],
            )
        return tools

    def _list_resources_callable(self, server_name: str):
        async def list_resources(cursor: str | None = None) -> dict[str, object]:
            return await self._list_resources(server_name, cursor)

        return list_resources

    def _list_templates_callable(self, server_name: str):
        async def list_resource_templates(cursor: str | None = None) -> dict[str, object]:
            return await self._list_resource_templates(server_name, cursor)

        return list_resource_templates

    def _read_resource_callable(self, server_name: str):
        async def read_resource(uri: str) -> dict[str, object]:
            return await self._read_resource(server_name, uri)

        return read_resource

    async def _list_resources(self, server_name: str, cursor: str | None) -> dict[str, object]:
        cursor = _validate_cursor(cursor)
        policy = self._policies[server_name]
        try:
            async with self._session(server_name) as session:
                result = await session.list_resources(cursor)
        except Exception:
            raise RuntimeError("MCP resource listing failed") from None
        resources = [
            {
                "name": item.name,
                "uri": str(item.uri),
                "description": item.description,
                "mime_type": item.mimeType,
                "size": item.size,
            }
            for item in result.resources
            if str(item.uri) in policy.resources
        ]
        return _bounded({"resources": resources, "next_cursor": result.nextCursor})

    async def _list_resource_templates(self, server_name: str, cursor: str | None) -> dict[str, object]:
        cursor = _validate_cursor(cursor)
        policy = self._policies[server_name]
        try:
            async with self._session(server_name) as session:
                result = await session.list_resource_templates(cursor)
        except Exception:
            raise RuntimeError("MCP resource template listing failed") from None
        templates = [
            {
                "name": item.name,
                "uri_template": item.uriTemplate,
                "description": item.description,
                "mime_type": item.mimeType,
            }
            for item in result.resourceTemplates
            if item.uriTemplate in policy.resource_templates
        ]
        return _bounded({"resource_templates": templates, "next_cursor": result.nextCursor})

    async def _read_resource(self, server_name: str, uri: str) -> dict[str, object]:
        if not isinstance(uri, str) or not uri or len(uri) > _MAX_URI_LENGTH:
            raise ValueError("MCP resource URI is invalid")
        policy = self._policies[server_name]
        if not policy.allows_uri(uri):
            raise ValueError("MCP resource URI is outside the versioned Harness allowlist")
        await self._require_advertised_policy(server_name)
        parsed_uri = _ANY_URL.validate_python(uri)
        try:
            async with self._session(server_name) as session:
                result = await session.read_resource(parsed_uri)
        except Exception:
            raise RuntimeError("MCP resource read failed") from None
        contents: list[dict[str, object]] = []
        for item in result.contents:
            if not isinstance(item, TextResourceContents):
                raise ValueError("Binary MCP resource content is not exposed to the Agent")
            item_uri = str(item.uri)
            if item_uri != uri or not policy.allows_uri(item_uri):
                raise ValueError("MCP server returned content for an unapproved URI")
            contents.append(
                {
                    "uri": item_uri,
                    "mime_type": item.mimeType,
                    "text": item.text,
                },
            )
        return _bounded({"contents": contents})

    async def _require_advertised_policy(self, server_name: str) -> None:
        async with self._roster_lock:
            cached = self._advertised.get(server_name)
            if cached is None:
                cached = await self._fetch_advertised_roster(server_name)
                self._advertised[server_name] = cached
        resources, templates = cached
        policy = self._policies[server_name]
        missing_resources = policy.resources - resources
        missing_templates = set(policy.resource_templates) - templates
        if missing_resources or missing_templates:
            raise RuntimeError("MCP server is missing resources required by the versioned Harness")

    async def _fetch_advertised_roster(
        self,
        server_name: str,
    ) -> tuple[frozenset[str], frozenset[str]]:
        resources: set[str] = set()
        templates: set[str] = set()
        try:
            async with self._session(server_name) as session:
                cursor: str | None = None
                seen: set[str] = set()
                for _ in range(_MAX_ROSTER_PAGES):
                    page = await session.list_resources(cursor)
                    resources.update(str(item.uri) for item in page.resources)
                    _require_roster_size(resources)
                    cursor = page.nextCursor
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise RuntimeError("MCP resource pagination cursor repeated")
                    seen.add(cursor)
                else:
                    raise RuntimeError("MCP resource roster exceeds the page limit")

                cursor = None
                seen.clear()
                for _ in range(_MAX_ROSTER_PAGES):
                    page = await session.list_resource_templates(cursor)
                    templates.update(item.uriTemplate for item in page.resourceTemplates)
                    _require_roster_size(templates)
                    cursor = page.nextCursor
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise RuntimeError("MCP resource template pagination cursor repeated")
                    seen.add(cursor)
                else:
                    raise RuntimeError("MCP resource template roster exceeds the page limit")
        except Exception:
            raise RuntimeError("MCP resource roster validation failed") from None
        return frozenset(resources), frozenset(templates)

    @asynccontextmanager
    async def _session(self, server_name: str) -> AsyncGenerator[ClientSession, None]:
        config = self._clients[server_name].mcp_config
        if not isinstance(config, HttpMCPConfig):
            raise ValueError("MCP resource facade only supports http_mcp")
        timeout = config.timeout or 30.0
        async with httpx.AsyncClient(
            headers=config.headers or {},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            async with streamable_http_client(
                config.url,
                http_client=http_client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=timeout),
                ) as session:
                    await session.initialize()
                    yield session


def parse_mcp_resource_policy(server_name: str, record: Mapping[str, object]) -> MCPResourcePolicy:
    """解析并严格校验 Harness 中的精确 resource allowlist。"""

    resources = _exact_string_list(record.get("enable_resources"), "enable_resources")
    templates = _exact_string_list(record.get("enable_resource_templates"), "enable_resource_templates")
    for uri in (*resources, *templates):
        if len(uri) > _MAX_URI_LENGTH:
            raise ValueError("MCP resource URI exceeds the limit")
        _ANY_URL.validate_python(_template_probe_uri(uri) if uri in templates else uri)
    return MCPResourcePolicy(
        server_name=server_name,
        resources=frozenset(resources),
        resource_templates=templates,
    )


def _exact_string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"MCP {field} must be an explicit string list")
    if len(value) != len(set(value)):
        raise ValueError(f"MCP {field} contains duplicates")
    return tuple(value)


def _template_probe_uri(template: str) -> str:
    matches = tuple(_TEMPLATE_VARIABLE.finditer(template))
    probe = _TEMPLATE_VARIABLE.sub("agentgov-probe", template)
    if not matches or "{" in probe or "}" in probe:
        raise ValueError("MCP resource template must use simple named variables")
    return probe


def _template_pattern(template: str) -> re.Pattern[str]:
    _template_probe_uri(template)
    chunks: list[str] = []
    position = 0
    for match in _TEMPLATE_VARIABLE.finditer(template):
        chunks.append(re.escape(template[position : match.start()]))
        chunks.append(r"[^/?#]+")
        position = match.end()
    chunks.append(re.escape(template[position:]))
    return re.compile("".join(chunks))


def _validate_cursor(cursor: str | None) -> str | None:
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > _MAX_CURSOR_LENGTH):
        raise ValueError("MCP pagination cursor is invalid")
    return cursor


def _bounded(payload: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_RESULT_BYTES:
        raise ValueError("MCP resource result exceeds the Runtime limit")
    return payload


def _require_roster_size(values: set[str]) -> None:
    if len(values) > _MAX_ROSTER_ENTRIES:
        raise RuntimeError("MCP resource roster exceeds the entry limit")
