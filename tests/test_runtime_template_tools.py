from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bootstrap_runtime_volume import LOCAL_DEBUG_RUNTIME_VOLUME_ROOT, bootstrap_runtime_volume, resolve_runtime_root  # noqa: E402
from export_runtime_template import _create_backup, export_runtime_template  # noqa: E402
from restore_runtime_template_backup import restore_backup  # noqa: E402
from runtime_cleanup import cleanup_runtime_artifacts  # noqa: E402
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


def test_export_runtime_template_excludes_private_runtime_state_and_cleans_artifacts(tmp_path):
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
    assert result["backup"] is None
    assert not backup_dir.exists()
    assert not staging_dir.exists()
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


def test_cleanup_runtime_artifacts_removes_backups_without_runtime_data(tmp_path):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "main-workspace"
    workspace.mkdir(parents=True)
    backup_dir = runtime_root / ".runtime-template-backups" / "20260608T000000Z"
    backup_dir.mkdir(parents=True)
    (backup_dir / "agent.yaml").write_text("backup", encoding="utf-8")
    old_mcp_backup = workspace / ".mcp.json.bak-20260608T000000Z"
    old_agent_backup = workspace / "agent.yaml.bak-20260608T000000Z"
    old_mcp_backup.write_text("backup", encoding="utf-8")
    old_agent_backup.write_text("backup", encoding="utf-8")
    runtime_data = runtime_root / "data" / "runtime.sqlite3"
    runtime_data_backup = runtime_root / "data" / "runtime.sqlite3.bak-20260608T000000Z"
    langfuse_backup = runtime_root / "langfuse" / "postgres.bak-20260608T000000Z"
    git_config = workspace / ".git" / "config"
    private_mcp = workspace / ".mcp.local.json"
    runtime_data.parent.mkdir(parents=True)
    runtime_data.write_text("sqlite", encoding="utf-8")
    runtime_data_backup.write_text("sqlite backup", encoding="utf-8")
    langfuse_backup.parent.mkdir(parents=True)
    langfuse_backup.write_text("langfuse backup", encoding="utf-8")
    git_config.parent.mkdir(parents=True)
    git_config.write_text("[core]\n", encoding="utf-8")
    private_mcp.write_text("{}\n", encoding="utf-8")

    result = cleanup_runtime_artifacts(runtime_root=runtime_root)

    assert str(runtime_root / ".runtime-template-backups") in result["removed"]
    assert old_mcp_backup.as_posix() in result["removed"]
    assert old_agent_backup.as_posix() in result["removed"]
    assert not (runtime_root / ".runtime-template-backups").exists()
    assert not old_mcp_backup.exists()
    assert not old_agent_backup.exists()
    assert runtime_data.exists()
    assert runtime_data_backup.exists()
    assert langfuse_backup.exists()
    assert git_config.exists()
    assert private_mcp.exists()


def test_runtime_template_safety_rejects_unrenderable_host_path_placeholder(tmp_path):
    template = tmp_path / "template"
    workspace = template / "main-workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text("paths:\n  workspace: ${HOST_PATH}\n", encoding="utf-8")

    findings = scan_path(template)

    assert any(finding.kind == "unrenderable_placeholder" and finding.severity == "high" for finding in findings)


def test_runtime_template_safety_rejects_backup_artifacts(tmp_path):
    template = tmp_path / "template"
    workspace = template / "main-workspace"
    backup_dir = workspace / ".runtime-template-backups"
    backup_dir.mkdir(parents=True)
    (backup_dir / ".mcp.json").write_text("{}\n", encoding="utf-8")
    (workspace / ".mcp.json.bak-20260608T000000Z").write_text("{}\n", encoding="utf-8")

    findings = scan_path(template)

    assert any(finding.path == "main-workspace/.runtime-template-backups/.mcp.json" for finding in findings)
    assert any(finding.path == "main-workspace/.mcp.json.bak-20260608T000000Z" for finding in findings)


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
    agents_dir = settings_template / "agents"
    commands_dir = settings_template / "commands"
    skill_dir = settings_template / "skills" / "report-generation"
    agents_dir.mkdir(parents=True)
    commands_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (agents_dir / "report-writer.md").write_text(
        "日报写入 `/data/outputs/reports/daily-secops-report-YYYY-MM-DD.md`。\n",
        encoding="utf-8",
    )
    (commands_dir / "generate-report.md").write_text(
        "生成报告到 `/data/outputs/reports`。\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "报告输出目录：`/data/outputs/reports`。\n",
        encoding="utf-8",
    )
    server_dir = workspace_template / "mcp_servers" / "report_template_mcp"
    server_dir.mkdir(parents=True)
    (server_dir / "server.py").write_text(
        'REPORT_TEMPLATE_DIR = "/main-workspace/templates/reports"\nREPORT_OUTPUT_DIR = "/data/outputs/reports"\n',
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
    report_writer = (runtime_root / "main-workspace" / ".claude" / "agents" / "report-writer.md").read_text(encoding="utf-8")
    report_command = (runtime_root / "main-workspace" / ".claude" / "commands" / "generate-report.md").read_text(encoding="utf-8")
    report_skill = (runtime_root / "main-workspace" / ".claude" / "skills" / "report-generation" / "SKILL.md").read_text(encoding="utf-8")
    report_server = (runtime_root / "main-workspace" / "mcp_servers" / "report_template_mcp" / "server.py").read_text(encoding="utf-8")
    assert result["validation_errors"] == []
    assert mcp["mcpServers"]["sec-ops-data"]["url"] == "http://localhost:58001/mcp"
    assert str(runtime_root / "data" / "outputs") in settings["permissions"]["allow"][0]
    assert str(runtime_root / "claude-roots" / "main" / ".claude.json") in settings["permissions"]["deny"][0]
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python \"$CLAUDE_PROJECT_DIR/hooks/pre_tool_guard.py\""
    assert settings["sandbox"]["network"]["allowedDomains"] == ["localhost", "127.0.0.1", "host.docker.internal", "*.internal", "*.corp"]
    assert f"workspace: {runtime_root / 'main-workspace'}" in agent
    assert "data_root: /data\n" not in agent
    assert f"`{runtime_root / 'data' / 'outputs' / 'reports' / 'daily-secops-report-YYYY-MM-DD.md'}`" in report_writer
    assert f"`{runtime_root / 'data' / 'outputs' / 'reports'}`" in report_command
    assert f"`{runtime_root / 'data' / 'outputs' / 'reports'}`" in report_skill
    assert f'"{runtime_root / "main-workspace" / "templates" / "reports"}"' in report_server
    assert f'"{runtime_root / "data" / "outputs" / "reports"}"' in report_server


def test_bootstrap_runtime_volume_keeps_container_paths_in_container_mode(tmp_path):
    template = tmp_path / "template"
    workspace_template = template / "main-workspace"
    agents_dir = workspace_template / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "report-writer.md").write_text(
        "日报写入 `/data/outputs/reports/daily-secops-report-YYYY-MM-DD.md`。\n",
        encoding="utf-8",
    )
    runtime_root = tmp_path / "container-runtime"

    result = bootstrap_runtime_volume(runtime_root=runtime_root, template_dir=template, runtime_volume_mode="container", env={})

    report_writer = (runtime_root / "main-workspace" / ".claude" / "agents" / "report-writer.md").read_text(encoding="utf-8")
    assert result["validation_errors"] == []
    assert "`/data/outputs/reports/daily-secops-report-YYYY-MM-DD.md`" in report_writer


def test_bootstrap_runtime_volume_repairs_managed_config_and_cleans_backups(tmp_path):
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
    assert not Path(result["backups"][0]).exists()
    assert ".runtime-template-backups" in result["backups"][0]
    assert (runtime_root / ".runtime-template-backups").as_posix() in result["cleanup_removed"]
    assert not (runtime_root / ".runtime-template-backups").exists()


def test_repair_managed_config_removes_stale_template_docs_without_runtime_data(tmp_path):
    template = tmp_path / "template"
    workspace_template = template / "main-workspace"
    workspace_template.mkdir(parents=True)
    (workspace_template / "CLAUDE.md").write_text("当前模板\n", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runtime_workspace = runtime_root / "main-workspace"
    runtime_workspace.mkdir(parents=True)
    stale_readme = runtime_workspace / "README.md"
    stale_doc = runtime_workspace / "docs" / "MCP_REPLACEMENT_GUIDE.md"
    stale_hook_readme = runtime_workspace / "hooks" / "README.md"
    runtime_data = runtime_root / "data" / "runtime.sqlite3"
    private_mcp = runtime_workspace / ".mcp.local.json"
    git_file = runtime_workspace / ".git" / "config"
    stale_readme.write_text("旧说明\n", encoding="utf-8")
    stale_doc.parent.mkdir(parents=True)
    stale_doc.write_text("旧 docs\n", encoding="utf-8")
    stale_hook_readme.parent.mkdir(parents=True)
    stale_hook_readme.write_text("旧 hook 说明\n", encoding="utf-8")
    runtime_data.parent.mkdir(parents=True)
    runtime_data.write_text("sqlite", encoding="utf-8")
    private_mcp.write_text("{}\n", encoding="utf-8")
    git_file.parent.mkdir(parents=True)
    git_file.write_text("[core]\n", encoding="utf-8")

    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=template,
        runtime_volume_mode="local-debug",
        env={},
        repair_managed_config=True,
    )

    assert not stale_readme.exists()
    assert not stale_doc.exists()
    assert not stale_hook_readme.exists()
    assert runtime_data.exists()
    assert private_mcp.exists()
    assert git_file.exists()
    assert result["removed"] == [stale_readme.as_posix(), stale_doc.as_posix(), stale_hook_readme.as_posix()]
    assert len(result["backups"]) == 3
    assert all(not Path(path).exists() for path in result["backups"])
    assert (runtime_root / ".runtime-template-backups").as_posix() in result["cleanup_removed"]
    assert not (runtime_root / ".runtime-template-backups").exists()


def test_resolve_runtime_root_uses_local_debug_mode_default(tmp_path):
    env_file = tmp_path / ".env.local-debug"
    env_file.write_text("", encoding="utf-8")

    assert resolve_runtime_root(None, env_file) == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT


def test_restore_runtime_template_backup_creates_pre_restore_backup(tmp_path):
    template = tmp_path / "template"
    template.mkdir()
    (template / "README.md").write_text("before", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_path = _create_backup(template, backup_dir)
    assert backup_path is not None
    (template / "README.md").write_text("changed", encoding="utf-8")

    restored = restore_backup(backup_path=backup_path, template_dir=template, backup_dir=backup_dir)

    assert restored["ok"] is True
    assert restored["pre_restore_backup"] is None
    assert restored["cleanup_removed"]
    assert not backup_dir.exists()
    assert (template / "README.md").read_text(encoding="utf-8") != "changed"
