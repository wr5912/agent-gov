from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.runtime.agent_profiles import GOVERNOR_PROFILE, build_business_agent_profile, build_profiles
from app.runtime.mcp_config import McpConfigError, filtered_mcp_servers, resolve_workspace_mcp_config_path
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.settings import AppSettings


def _write_mcp(path: Path, url: str) -> None:
    path.write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": url}}}),
        encoding="utf-8",
    )


def test_workspace_mcp_resolution_uses_project_config(tmp_path: Path) -> None:
    workspace = tmp_path / "main-workspace"
    workspace.mkdir()
    _write_mcp(workspace / ".mcp.json", "http://project.example/mcp")
    _write_mcp(workspace / ".mcp.local.json", "http://127.0.0.1:58001/mcp")

    resolution = resolve_workspace_mcp_config_path(workspace)
    servers = filtered_mcp_servers(resolution.path, ("sec-ops-data",), {})

    assert resolution.path == workspace / ".mcp.json"
    assert resolution.source == "workspace_project"
    assert servers == {"sec-ops-data": {"type": "http", "url": "http://project.example/mcp"}}


def test_filtered_mcp_servers_expands_env_and_rejects_unresolved_placeholder(tmp_path: Path) -> None:
    config_path = tmp_path / ".mcp.json"
    _write_mcp(config_path, "${MCP_SERVER_URL}")

    servers = filtered_mcp_servers(config_path, ("sec-ops-data",), {"MCP_SERVER_URL": "http://mcp.local/mcp"})

    assert servers == {"sec-ops-data": {"type": "http", "url": "http://mcp.local/mcp"}}
    with pytest.raises(McpConfigError, match="MCP_SERVER_URL"):
        filtered_mcp_servers(config_path, ("sec-ops-data",), {})


def test_filtered_mcp_servers_supports_default_placeholder_syntax(tmp_path: Path) -> None:
    config_path = tmp_path / ".mcp.json"
    _write_mcp(config_path, "${MCP_SERVER_URL:-http://default.local/mcp}")

    servers = filtered_mcp_servers(config_path, ("sec-ops-data",), {})

    assert servers == {"sec-ops-data": {"type": "http", "url": "http://default.local/mcp"}}


def test_filtered_mcp_servers_expands_default_placeholder_in_config_path(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime-root"
    workspace = runtime_root / "main-workspace"
    workspace.mkdir(parents=True)
    _write_mcp(workspace / ".mcp.json", "http://127.0.0.1:58001/mcp")

    servers = filtered_mcp_servers(
        Path(f"${{RUNTIME_ROOT:-{runtime_root}}}/main-workspace/.mcp.json"),
        ("sec-ops-data",),
        {},
    )

    assert servers == {"sec-ops-data": {"type": "http", "url": "http://127.0.0.1:58001/mcp"}}


def test_unresolved_mcp_config_path_fails_before_sdk_options(tmp_path: Path) -> None:
    with pytest.raises(McpConfigError, match="HOST_RUNTIME_VOLUME_ROOT"):
        filtered_mcp_servers(Path("${HOST_RUNTIME_VOLUME_ROOT}/main-workspace/.mcp.json"), ("sec-ops-data",), {})


def test_default_business_profile_uses_project_mcp_without_polluting_governor(tmp_path: Path) -> None:
    settings = AppSettings(_env_file=None, DATA_DIR=tmp_path / "data")
    workspace = settings.default_workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    _write_mcp(workspace / ".mcp.json", "http://project.example/mcp")
    _write_mcp(workspace / ".mcp.local.json", "http://127.0.0.1:58001/mcp")

    profiles = build_profiles(settings)

    business = build_business_agent_profile(settings, agent_id=DEFAULT_BUSINESS_AGENT_ID, workspace_dir=workspace)
    assert business.mcp_config_path == workspace / ".mcp.json"
    assert profiles[GOVERNOR_PROFILE].mcp_config_path.name == ".mcp.json"
