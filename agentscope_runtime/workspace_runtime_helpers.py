"""Small validation helpers kept outside the Runtime workspace lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import yaml
from agentgov_agentscope_contract import RuntimeTemplateRestartRequired
from agentscope.app import SubAgentTemplate
from agentscope.mcp import MCPClient

from .subagent_templates import load_subagent_templates


class McpWorkspace(Protocol):
    default_mcps: list[MCPClient]

    async def list_mcps(
        self,
        *,
        agent_id: str,
        session_id: str,
    ) -> list[MCPClient]: ...


def register_subagent_templates(
    workspace: Path,
    digest: str,
    registered: Mapping[str, SubAgentTemplate],
) -> None:
    """Reject templates that were absent or changed after Runtime startup."""

    for template_type, template in load_subagent_templates(workspace, digest).items():
        existing = registered.get(template_type)
        if existing is None:
            raise RuntimeTemplateRestartRequired()
        if existing != template:
            raise ValueError(f"Subagent template changed after Runtime startup: {template_type}")


def sandbox_proxy_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Copy credential-free proxy settings into a sandbox environment."""

    proxies: dict[str, str] = {}
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy"):
        value = environ.get(name)
        if not value:
            continue
        if name.lower() in {"http_proxy", "https_proxy"}:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Credential-bearing sandbox proxy URLs are forbidden")
        proxies[name] = value
    return proxies


async def validate_live_mcp_tools(
    workspace: McpWorkspace,
    *,
    agent_id: str,
    session_id: str,
) -> None:
    """Require the public live MCP roster to match the Harness allowlist."""

    clients = await workspace.list_mcps(agent_id=agent_id, session_id=session_id)
    expected_servers = {client.name for client in workspace.default_mcps}
    if {client.name for client in clients} != expected_servers:
        raise RuntimeError("MCP server roster does not match the versioned Harness")
    for client in clients:
        expected_tools = client.enable_tools
        if expected_tools is None:
            raise RuntimeError("MCP exact tool allowlist is missing")
        actual = [tool.name for tool in await client.list_raw_tools()]
        if len(actual) != len(set(actual)) or set(actual) != set(expected_tools):
            raise RuntimeError("MCP tool roster does not match the versioned Harness")


def load_network_hosts(workspace: Path, environ: Mapping[str, str]) -> frozenset[str]:
    """Resolve the Harness network policy without accepting wildcard targets."""

    manifest = workspace / "agent.yaml"
    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("agent.yaml network policy is unreadable") from exc
    policy = payload.get("workspace_policy") if isinstance(payload, dict) else None
    declarations = policy.get("allowed_network_domains") if isinstance(policy, dict) else None
    if not isinstance(declarations, list) or any(not isinstance(item, str) or not item for item in declarations):
        raise ValueError("workspace_policy.allowed_network_domains must be a string list")
    hosts: set[str] = set()
    for declaration in declarations:
        resolved = declaration
        if declaration.startswith("${") and declaration.endswith("}"):
            resolved = environ.get(declaration[2:-1], "")
            if not resolved:
                raise ValueError(f"Network policy environment is missing: {declaration[2:-1]}")
        parsed = urlsplit(resolved if "://" in resolved else f"https://{resolved}")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("workspace_policy.allowed_network_domains contains an unsafe target")
        if any(character in parsed.hostname for character in "*?[]"):
            raise ValueError("workspace_policy.allowed_network_domains cannot contain wildcards")
        hosts.add(parsed.hostname.lower())
    return frozenset(hosts)
