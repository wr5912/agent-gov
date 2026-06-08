from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bootstrap_runtime_volume import LOCAL_DEBUG_RUNTIME_VOLUME_ROOT, bootstrap_runtime_volume, resolve_runtime_root  # noqa: E402
from export_runtime_template import export_runtime_template  # noqa: E402
from restore_runtime_template_backup import restore_backup  # noqa: E402
from runtime_template_safety import sanitize_path, scan_path  # noqa: E402


def test_runtime_template_safety_sanitizes_network_and_secret_values(tmp_path):
    template = tmp_path / "template"
    workspace = template / "main-workspace"
    workspace.mkdir(parents=True)
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "soc": {
                        "type": "http",
                        "url": "http://10.0.0.2:58001/mcp",
                        "headers": {"Authorization": "Bearer private-token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    sample_dir = workspace / "mcp_servers" / "soc_data_mcp"
    sample_dir.mkdir(parents=True)
    (sample_dir / "sample_alerts.json").write_text(
        json.dumps([{"host": {"ip": "10.0.12.34"}, "network": {"dst_ip": "10.0.20.8", "dst_port": 443}}]),
        encoding="utf-8",
    )
    (workspace / "CLAUDE.local.md.example").write_text(
        "- SOC API: `http://host.docker.internal:8080`\n",
        encoding="utf-8",
    )

    assert any(finding.severity == "high" for finding in scan_path(template))
    sanitize_path(template)

    mcp = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["soc"]["url"] == "${MCP_SERVER_URL}"
    assert mcp["mcpServers"]["soc"]["headers"]["Authorization"] == "Bearer ${AUTH_TOKEN}"
    sample = json.loads((sample_dir / "sample_alerts.json").read_text(encoding="utf-8"))
    assert sample[0]["host"]["ip"] == "192.0.2.10"
    assert sample[0]["network"]["dst_ip"] == "192.0.2.10"
    assert sample[0]["network"]["dst_port"] == "${SERVICE_PORT}"
    assert (workspace / "CLAUDE.local.md.example").read_text(encoding="utf-8") == "- SOC API: `${SERVICE_URL}`\n"
    assert scan_path(template) == []


def test_export_runtime_template_excludes_private_runtime_state_and_backs_up(tmp_path):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "main-workspace"
    workspace.mkdir(parents=True)
    (workspace / ".mcp.json").write_text('{"mcpServers":{"soc":{"type":"http","url":"http://10.0.0.2:58001/mcp"}}}', encoding="utf-8")
    (workspace / "agent.yaml").write_text(
        f"paths:\n  workspace: {workspace}\n  claude_home: {runtime_root / 'claude-roots' / 'main' / '.claude'}\n  data_root: {runtime_root / 'data'}\n",
        encoding="utf-8",
    )
    (workspace / ".mcp.local.json").write_text('{"mcpServers":{"soc":{"url":"http://10.0.0.3:58001/mcp"}}}', encoding="utf-8")
    (workspace / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
    (runtime_root / "data").mkdir()
    (runtime_root / "data" / "runtime.sqlite3").write_text("sqlite", encoding="utf-8")

    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "README.md").write_text("old", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"

    result = export_runtime_template(
        runtime_root=runtime_root,
        template_dir=template_dir,
        backup_dir=backup_dir,
        staging_root=staging_dir,
    )

    assert result["ok"] is True
    assert result["backup"]
    assert (template_dir / "README.md").exists()
    assert not (template_dir / "main-workspace" / ".mcp.local.json").exists()
    assert not (template_dir / "main-workspace" / ".env").exists()
    assert not (template_dir / "main-workspace" / ".git" / "config").exists()
    assert not (template_dir / "data" / "runtime.sqlite3").exists()
    mcp = json.loads((template_dir / "main-workspace" / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["soc"]["url"] == "${MCP_SERVER_URL}"
    agent = (template_dir / "main-workspace" / "agent.yaml").read_text(encoding="utf-8")
    assert "workspace: /main-workspace" in agent
    assert "claude_home: /claude-roots/main/.claude" in agent
    assert "data_root: /data" in agent
    assert "${HOST_PATH}" not in agent


def test_runtime_template_safety_rejects_unrenderable_host_path_placeholder(tmp_path):
    template = tmp_path / "template"
    workspace = template / "main-workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text("paths:\n  workspace: ${HOST_PATH}\n", encoding="utf-8")

    findings = scan_path(template)

    assert any(finding.kind == "unrenderable_placeholder" and finding.severity == "high" for finding in findings)


def test_bootstrap_runtime_volume_fills_missing_without_overwrite(tmp_path):
    template = tmp_path / "template"
    (template / "main-workspace").mkdir(parents=True)
    (template / "main-workspace" / "CLAUDE.md").write_text("template", encoding="utf-8")
    (template / "main-workspace" / "agent.yaml").write_text("agent", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    (runtime_root / "main-workspace").mkdir(parents=True)
    (runtime_root / "main-workspace" / "CLAUDE.md").write_text("custom", encoding="utf-8")

    result = bootstrap_runtime_volume(runtime_root=runtime_root, template_dir=template)

    assert (runtime_root / "main-workspace" / "CLAUDE.md").read_text(encoding="utf-8") == "custom"
    assert (runtime_root / "main-workspace" / "agent.yaml").read_text(encoding="utf-8") == "agent"
    assert (runtime_root / "data" / "outputs" / "reports").is_dir()
    assert (runtime_root / "data" / "agent-governance" / "worktrees").is_dir()
    assert result["skipped_existing"]


def test_bootstrap_runtime_volume_renders_local_debug_managed_config(tmp_path):
    template = tmp_path / "template"
    workspace_template = template / "main-workspace"
    settings_template = workspace_template / ".claude"
    settings_template.mkdir(parents=True)
    (workspace_template / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": "${MCP_SERVER_URL}"}}}),
        encoding="utf-8",
    )
    (settings_template / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Write(/data/outputs/**)"], "deny": ["Read(/claude-roots/main/.claude.json)"]},
                "hooks": {"PreToolUse": [{"hooks": [{"command": "python \"$CLAUDE_PROJECT_DIR/hooks/pre_tool_guard.py\""}]}]},
                "sandbox": {
                    "filesystem": {"allowWrite": ["/data/outputs"], "denyRead": ["/claude-roots/main/.claude.json"]},
                    "network": {"allowedDomains": ["${SERVICE_HOST}", "${INTERNAL_DOMAIN}"]},
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace_template / "agent.yaml").write_text("paths:\n  workspace: /main-workspace\n  data_root: /data\n", encoding="utf-8")
    runtime_root = tmp_path / "local-debug-runtime"
    env = {
        "MAIN_WORKSPACE_DIR": str(runtime_root / "main-workspace"),
        "MAIN_CLAUDE_ROOT": str(runtime_root / "claude-roots" / "main"),
        "DATA_DIR": str(runtime_root / "data"),
        "MCP_SERVER_URL": "http://localhost:58001/mcp",
    }

    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=template,
        runtime_volume_mode="local-debug",
        env=env,
    )

    mcp = json.loads((runtime_root / "main-workspace" / ".mcp.json").read_text(encoding="utf-8"))
    settings = json.loads((runtime_root / "main-workspace" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    agent = (runtime_root / "main-workspace" / "agent.yaml").read_text(encoding="utf-8")
    assert result["validation_errors"] == []
    assert mcp["mcpServers"]["sec-ops-data"]["url"] == "http://localhost:58001/mcp"
    assert str(runtime_root / "data" / "outputs") in settings["permissions"]["allow"][0]
    assert str(runtime_root / "claude-roots" / "main" / ".claude.json") in settings["permissions"]["deny"][0]
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python \"$CLAUDE_PROJECT_DIR/hooks/pre_tool_guard.py\""
    assert settings["sandbox"]["network"]["allowedDomains"] == ["localhost", "127.0.0.1", "host.docker.internal", "*.internal", "*.corp"]
    assert f"workspace: {runtime_root / 'main-workspace'}" in agent
    assert "data_root: /data\n" not in agent


def test_bootstrap_runtime_volume_repairs_managed_config_with_backup(tmp_path):
    template = tmp_path / "template"
    workspace_template = template / "main-workspace"
    workspace_template.mkdir(parents=True)
    (workspace_template / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": "${MCP_SERVER_URL}"}}}),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime"
    runtime_workspace = runtime_root / "main-workspace"
    runtime_workspace.mkdir(parents=True)
    (runtime_workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": "${MCP_SERVER_URL}"}}}),
        encoding="utf-8",
    )

    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=template,
        runtime_volume_mode="local-debug",
        env={"MCP_SERVER_URL": "http://localhost:58001/mcp"},
        repair_managed_config=True,
    )

    mcp = json.loads((runtime_workspace / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["sec-ops-data"]["url"] == "http://localhost:58001/mcp"
    assert result["repaired"] == [(runtime_workspace / ".mcp.json").as_posix()]
    assert result["backups"]
    assert Path(result["backups"][0]).exists()


def test_resolve_runtime_root_uses_local_debug_mode_default(tmp_path):
    env_file = tmp_path / ".env.local-debug"
    env_file.write_text("", encoding="utf-8")

    assert resolve_runtime_root(None, env_file) == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT


def test_restore_runtime_template_backup_creates_pre_restore_backup(tmp_path):
    template = tmp_path / "template"
    template.mkdir()
    (template / "README.md").write_text("before", encoding="utf-8")
    backup_dir = tmp_path / "backups"

    first = export_runtime_template(
        runtime_root=tmp_path / "missing-runtime",
        template_dir=template,
        backup_dir=backup_dir,
        staging_root=tmp_path / "staging-1",
    )
    assert first["ok"] is True
    (template / "README.md").write_text("changed", encoding="utf-8")

    restored = restore_backup(backup_path=Path(first["backup"]), template_dir=template, backup_dir=backup_dir)

    assert restored["ok"] is True
    assert restored["pre_restore_backup"]
    assert (template / "README.md").read_text(encoding="utf-8") != "changed"
