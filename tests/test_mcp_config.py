from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.runtime.agent_profiles import MAIN_AGENT_PROFILE, build_profiles
from app.runtime.mcp_config import McpConfigError, filtered_mcp_servers, resolve_main_mcp_config_path
from app.runtime.settings import AppSettings


def _write_mcp(path: Path, url: str) -> None:
    path.write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": url}}}),
        encoding="utf-8",
    )


def test_main_mcp_resolution_prefers_workspace_local_override(tmp_path: Path) -> None:
    workspace = tmp_path / "main-workspace"
    workspace.mkdir()
    _write_mcp(workspace / ".mcp.json", "${MCP_SERVER_URL}")
    _write_mcp(workspace / ".mcp.local.json", "http://127.0.0.1:58001/mcp")

    resolution = resolve_main_mcp_config_path(workspace, None)
    servers = filtered_mcp_servers(resolution.path, ("sec-ops-data",), {})

    assert resolution.path == workspace / ".mcp.local.json"
    assert resolution.source == "workspace_local"
    assert servers == {"sec-ops-data": {"type": "http", "url": "http://127.0.0.1:58001/mcp"}}


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


def test_explicit_host_workspace_path_maps_to_container_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "main-workspace"
    workspace.mkdir()
    _write_mcp(workspace / ".mcp.local.json", "http://127.0.0.1:58001/mcp")

    resolution = resolve_main_mcp_config_path(
        workspace,
        Path("/host/runtime-root/main-workspace/.mcp.local.json"),
    )

    assert resolution.path == workspace / ".mcp.local.json"
    assert resolution.source == "explicit_env_workspace_mount"


def test_main_profile_uses_local_mcp_without_polluting_feedback_profiles(tmp_path: Path) -> None:
    workspace = tmp_path / "main-workspace"
    workspace.mkdir()
    _write_mcp(workspace / ".mcp.local.json", "http://127.0.0.1:58001/mcp")
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "data",
        MAIN_WORKSPACE_DIR=workspace,
        MAIN_CLAUDE_ROOT=tmp_path / "claude-roots" / "main",
    )

    profiles = build_profiles(settings)

    assert profiles[MAIN_AGENT_PROFILE].mcp_config_path == workspace / ".mcp.local.json"
    assert profiles["attribution-analyzer"].mcp_config_path.name == ".mcp.json"


def test_blank_optional_mcp_config_path_uses_workspace_resolution(tmp_path: Path) -> None:
    workspace = tmp_path / "main-workspace"
    workspace.mkdir()
    _write_mcp(workspace / ".mcp.local.json", "http://127.0.0.1:58001/mcp")
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "data",
        MAIN_WORKSPACE_DIR=workspace,
        MAIN_CLAUDE_ROOT=tmp_path / "claude-roots" / "main",
        CLAUDE_MCP_CONFIG_PATH="",
    )

    profiles = build_profiles(settings)

    assert settings.claude_mcp_config_path is None
    assert profiles[MAIN_AGENT_PROFILE].mcp_config_path == workspace / ".mcp.local.json"
