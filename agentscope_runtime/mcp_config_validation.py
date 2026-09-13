"""Validate governed HTTP MCP declarations before Runtime client creation."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from agentscope.mcp import HttpMCPConfig

from .types import JsonObject

HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}")
_FORBIDDEN_MCP_HEADERS = {
    "connection",
    "content-length",
    "forwarded",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "via",
    "x-original-url",
    "x-rewrite-url",
}
_RESERVED_RESOURCE_TOOL_NAMES = {
    "resources_list",
    "resource_templates_list",
    "resource_read",
}


def mcp_env_prefix(server_name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", server_name.upper()).strip("_")
    return f"{normalized}_MCP_"


def forbidden_mcp_header(name: str) -> bool:
    normalized = name.casefold()
    return normalized in _FORBIDDEN_MCP_HEADERS or normalized.startswith("x-forwarded-")


def validated_http_mcp_config(
    config: JsonObject,
    allowed_hosts: frozenset[str],
    *,
    require_container_route: bool,
) -> HttpMCPConfig:
    parsed = HttpMCPConfig.model_validate(config)
    parsed_url = urlsplit(parsed.url)
    if not _visible_ascii(parsed.url):
        raise ValueError("HTTP MCP URL must contain visible ASCII only")
    _validate_mcp_ip_address(parsed_url.hostname, require_container_route=require_container_route)
    headers = parsed.headers or {}
    if any(not _visible_ascii(name) or not _visible_ascii(value) for name, value in headers.items()):
        raise ValueError("HTTP MCP headers must contain visible ASCII only")
    if (
        parsed_url.scheme not in {"http", "https"}
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.hostname is None
        or parsed_url.hostname.lower() not in allowed_hosts
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("HTTP MCP URL is outside workspace_policy.allowed_network_domains")
    if parsed_url.path.rstrip("/") != "/mcp":
        raise ValueError("AgentGov MCP resource facade requires the streamable HTTP /mcp endpoint")
    if require_container_route and parsed_url.scheme == "http" and parsed_url.hostname.casefold() != "host.docker.internal":
        raise ValueError("Container Runtime permits plaintext MCP only through host.docker.internal")
    if require_container_route and parsed_url.hostname.casefold() == "localhost":
        raise ValueError("Container Runtime MCP URL cannot use localhost")
    return parsed


def explicit_mcp_tools(record: JsonObject) -> list[str]:
    enable_tools = record.get("enable_tools")
    if (
        not isinstance(enable_tools, list)
        or any(
            not isinstance(tool_name, str) or _MCP_TOOL_NAME.fullmatch(tool_name) is None or tool_name in _RESERVED_RESOURCE_TOOL_NAMES
            for tool_name in enable_tools
        )
        or len(enable_tools) != len(set(enable_tools))
    ):
        raise ValueError("MCP enable_tools must be an explicit unique tool-name list")
    return enable_tools


def _visible_ascii(value: str) -> bool:
    return bool(value) and all(0x20 <= ord(character) <= 0x7E for character in value)


def _validate_mcp_ip_address(hostname: str | None, *, require_container_route: bool) -> None:
    try:
        address = ipaddress.ip_address(hostname or "")
    except ValueError:
        return
    if not (address.is_global or address.is_loopback):
        raise ValueError("HTTP MCP URL uses a forbidden IP address")
    if require_container_route and address.is_loopback:
        raise ValueError("Container Runtime MCP URL cannot use a loopback address")
