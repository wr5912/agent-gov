from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bootstrap_runtime_volume import LOCAL_DEBUG_RUNTIME_VOLUME_ROOT, bootstrap_runtime_volume, resolve_runtime_root  # noqa: E402
from runtime_cleanup import cleanup_runtime_artifacts  # noqa: E402
from runtime_template_safety import sanitize_path, scan_path  # noqa: E402


def test_runtime_template_safety_scan_is_read_only(tmp_path):
    template = tmp_path / "template"
    mcp_path = template / "data" / "business-agents" / "support-agent" / "workspace" / ".mcp.json"
    mcp_path.parent.mkdir(parents=True)
    original = b'{"mcpServers":{"support":{"url":"https://user:secret@support.example/mcp"}}}\n'
    mcp_path.write_bytes(original)

    findings = scan_path(template)

    assert any(finding.severity == "high" for finding in findings)
    assert mcp_path.read_bytes() == original


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


def test_cleanup_runtime_artifacts_removes_backups_without_runtime_data(tmp_path):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "main-workspace"
    workspace.mkdir(parents=True)
    backup_dir = runtime_root / ".runtime-volume-seeds-backups" / "20260608T000000Z"
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

    assert str(runtime_root / ".runtime-volume-seeds-backups") in result["removed"]
    assert old_mcp_backup.as_posix() in result["removed"]
    assert old_agent_backup.as_posix() in result["removed"]
    assert not (runtime_root / ".runtime-volume-seeds-backups").exists()
    assert not old_mcp_backup.exists()
    assert not old_agent_backup.exists()
    assert runtime_data.exists()
    assert runtime_data_backup.exists()
    assert langfuse_backup.exists()
    assert git_config.exists()
    assert private_mcp.exists()


def test_cleanup_runtime_artifacts_removes_retired_template_tool_artifacts(tmp_path):
    template_dir = tmp_path / "docker" / "runtime-volume-seeds"
    template_dir.mkdir(parents=True)
    (template_dir / "README.md").write_text("current\n", encoding="utf-8")
    retired_artifacts = [
        template_dir.parent / ".runtime-volume-seeds-backups",
        template_dir.parent / ".runtime-volume-seeds-staging",
        template_dir.parent / ".runtime-volume-seeds.restore",
        template_dir.parent / ".runtime-volume-seeds.before-restore",
        template_dir.parent / ".runtime-volume-seeds.old-20260608T000000Z",
    ]
    for path in retired_artifacts:
        path.mkdir()

    result = cleanup_runtime_artifacts(template_dir=template_dir)

    assert set(result["removed"]) == {path.as_posix() for path in retired_artifacts}
    assert all(not path.exists() for path in retired_artifacts)
    assert (template_dir / "README.md").read_text(encoding="utf-8") == "current\n"


def test_runtime_template_safety_rejects_unrenderable_host_path_placeholder(tmp_path):
    template = tmp_path / "template"
    workspace = template / "main-workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text("paths:\n  workspace: ${HOST_PATH}\n", encoding="utf-8")

    findings = scan_path(template)

    assert any(finding.kind == "unrenderable_placeholder" and finding.severity == "high" for finding in findings)


def test_runtime_template_safety_allows_mode_neutral_sandbox_domains_only_in_settings(tmp_path):
    template = tmp_path / "template"
    settings_path = template / "data" / "business-agents" / "main-agent" / "workspace" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "sandbox": {
                    "network": {
                        "allowedDomains": [
                            "localhost",
                            "127.0.0.1",
                            "host.docker.internal",
                            "*.internal",
                            "*.corp",
                        ]
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert scan_path(template) == []
    assert sanitize_path(template)["changed"] == []

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["permissions"] = {"allow": ["host.docker.internal"]}
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    assert any(finding.kind == "local_host" for finding in scan_path(template))


def test_runtime_template_safety_warns_for_declared_seed_endpoint_but_blocks_embedded_secret(tmp_path):
    template = tmp_path / "template"
    mcp_path = template / "data" / "business-agents" / "support-agent" / "workspace" / ".mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"support": {"type": "http", "url": "http://support.internal:58001/mcp"}}}),
        encoding="utf-8",
    )

    findings = scan_path(template)

    assert any(finding.kind == "endpoint_url" and finding.severity == "medium" for finding in findings)
    assert any(finding.kind == "internal_domain" and finding.severity == "medium" for finding in findings)
    assert not any(finding.severity == "high" for finding in findings)

    mcp_path.write_text(
        json.dumps({"mcpServers": {"support": {"type": "http", "url": "https://user:secret@support.example/mcp"}}}),
        encoding="utf-8",
    )

    findings = scan_path(template)

    assert any(finding.kind == "secret" and finding.severity == "high" for finding in findings)


def test_runtime_template_safety_blocks_common_repo_secret_and_private_path_shapes(tmp_path):
    template = tmp_path / "template"
    workspace = template / "data" / "business-agents" / "support-agent" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "credentials.txt").write_text(
        "\n".join(
            (
                "github_pat_" + "EXAMPLE12345678901234567890",
                "ghp_" + "EXAMPLE12345678901234567890",
                "-----BEGIN " + "PRIVATE KEY-----",
                "postgresql://user:" + "example-password@database.example/db",
                "/root/.config/private-tool/config.json",
                r"C:\Users\example-user\private-tool\config.json",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (workspace / "credentials.json").write_text(
        json.dumps(
            {
                "headers": {
                    "X-Api-Key": "example-key-value",
                    "Authorization": "Basic example-credential",
                },
                "database": {"password": "example-password"},
                "allowed": {"api_key": "${API_KEY}"},
            }
        ),
        encoding="utf-8",
    )
    (workspace / "credentials.yaml").write_text(
        '"Authorization": "Basic yaml-example-credential"\ndatabase:\n  "password": yaml-example-password\nallowed:\n  api_key: ${API_KEY}\n',
        encoding="utf-8",
    )

    findings = scan_path(template)

    high_kinds = {finding.kind for finding in findings if finding.severity == "high"}
    assert {"secret", "private_key", "host_path"} <= high_kinds
    structured_paths = {finding.snippet for finding in findings if finding.message.startswith("credential-bearing structured")}
    assert {
        "headers.X-Api-Key=<VALUE>",
        "headers.Authorization=<VALUE>",
        "database.password=<VALUE>",
    } <= structured_paths
    assert "allowed.api_key=<VALUE>" not in structured_paths
    assert all("EXAMPLE12345678901234567890" not in finding.snippet for finding in findings)
    assert all("example-password" not in finding.snippet for finding in findings)


@pytest.mark.parametrize("suffix", [".js", ".ts"])
def test_runtime_template_safety_scans_utf8_text_without_suffix_allowlist(tmp_path, suffix):
    template = tmp_path / "template"
    workspace = template / "data" / "business-agents" / "support-agent" / "workspace"
    workspace.mkdir(parents=True)
    source_path = workspace / f"client{suffix}"
    source_path.write_text(
        (
            'export const api_key = "example-runtime-secret";\n'
            'export const client = {"apiKey": "plain-object-secret", '
            '"Authorization": "Basic object-credential"};\n'
            'process.env["API_KEY"] = "process-env-secret";\n'
            'config["apiKey"] = "config-secret";\n'
            'headers["Authorization"] = "Basic header-credential";\n'
            'export const computed = {["api_key"]: "computed-secret"};\n'
        ),
        encoding="utf-8",
    )

    findings = scan_path(template)

    secret_findings = [
        finding for finding in findings if finding.path.endswith(f"workspace/client{suffix}") and finding.kind == "secret" and finding.severity == "high"
    ]
    assert len(secret_findings) >= 6
    for secret in (
        "example-runtime-secret",
        "plain-object-secret",
        "object-credential",
        "process-env-secret",
        "config-secret",
        "header-credential",
        "computed-secret",
    ):
        assert all(secret not in finding.snippet for finding in findings)


def test_runtime_template_safety_skips_nul_binary_content(tmp_path):
    template = tmp_path / "template"
    workspace = template / "data" / "business-agents" / "support-agent" / "workspace"
    workspace.mkdir(parents=True)
    binary_path = workspace / "asset.bin"
    binary_path.write_bytes(b"\x00api_key=example-runtime-secret")

    findings = scan_path(template)

    assert not any(finding.path.endswith("workspace/asset.bin") for finding in findings)


@pytest.mark.parametrize(
    "file_name",
    [
        "runtime.db-journal",
        "runtime.db-shm",
        "runtime.db-wal",
        "runtime.sqlite-journal",
        "runtime.sqlite-shm",
        "runtime.sqlite-wal",
        "runtime.sqlite3-journal",
        "runtime.sqlite3-shm",
        "runtime.sqlite3-wal",
    ],
)
def test_runtime_template_safety_rejects_sqlite_sidecars(tmp_path, file_name):
    template = tmp_path / "template"
    workspace = template / "main-workspace"
    workspace.mkdir(parents=True)
    sidecar_path = workspace / file_name
    sidecar_path.write_bytes(b"runtime database state")

    findings = scan_path(template)

    assert any(finding.path == f"main-workspace/{file_name}" and finding.kind == "forbidden_path" and finding.severity == "high" for finding in findings)


def test_runtime_template_safety_warns_for_declared_seed_wide_allow_rules(tmp_path):
    template = tmp_path / "template"
    settings_path = template / "data" / "business-agents" / "support-agent" / "workspace" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Bash(*)",
                        "mcp__*__*",
                        "mcp__support__*",
                        "Bash(pwd)",
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    findings = scan_path(template)

    warnings = [finding for finding in findings if finding.kind == "wide_permission"]
    assert {finding.snippet for finding in warnings} == {
        "Bash(*)",
        "mcp__*__*",
        "mcp__support__*",
    }
    assert all(finding.severity == "medium" for finding in warnings)
    assert not any(finding.severity == "high" for finding in findings)


def test_runtime_template_safety_and_bootstrap_reject_seed_symlinks(tmp_path):
    template = tmp_path / "template"
    workspace = template / "data" / "business-agents" / "support-agent" / "workspace"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    linked = workspace / "linked.txt"
    linked.symlink_to(outside)

    findings = scan_path(template)

    assert any(finding.path.endswith("workspace/linked.txt") and finding.kind == "unsafe_file_type" and finding.severity == "high" for finding in findings)
    with pytest.raises(ValueError, match="regular file or directory"):
        bootstrap_runtime_volume(
            runtime_root=tmp_path / "runtime",
            template_dir=template,
        )


def test_runtime_template_safety_keeps_generic_template_endpoints_strict(tmp_path):
    template = tmp_path / "template"
    mcp_path = template / "templates" / "business-agent" / "general" / ".mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"support": {"type": "http", "url": "https://support.example/mcp"}}}),
        encoding="utf-8",
    )

    findings = scan_path(template)

    assert any(finding.kind == "endpoint_url" and finding.severity == "high" for finding in findings)


def test_runtime_template_safety_rejects_backup_artifacts(tmp_path):
    template = tmp_path / "template"
    workspace = template / "main-workspace"
    backup_dir = workspace / ".runtime-volume-seeds-backups"
    backup_dir.mkdir(parents=True)
    (backup_dir / ".mcp.json").write_text("{}\n", encoding="utf-8")
    (workspace / ".mcp.json.bak-20260608T000000Z").write_text("{}\n", encoding="utf-8")

    findings = scan_path(template)

    assert any(finding.path == "main-workspace/.runtime-volume-seeds-backups/.mcp.json" for finding in findings)
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
    # main-agent 的 version 骨架不再被无条件创建：main 是可删除的普通业务 Agent，固定目录会在
    # 它被删除后每次启动重建骨架、使删除不粘。该目录由 GitAgentVersionStore.ensure_bootstrap
    # 在真正需要版本库时按需建立。
    assert not (runtime_root / "data" / "business-agents" / "main-agent" / "version").exists()
    assert result["skipped_existing"]


def test_bootstrap_runtime_volume_migrates_legacy_agent_governance_dirs(tmp_path):
    template = tmp_path / "template"
    template.mkdir()
    runtime_root = tmp_path / "runtime"
    legacy_worktree = runtime_root / "data" / "agent-governance" / "worktrees" / "cs-old"
    legacy_release = runtime_root / "data" / "agent-governance" / "releases" / "release-old.tar.gz"
    legacy_worktree.mkdir(parents=True)
    legacy_release.parent.mkdir(parents=True)
    legacy_release.write_text("archive", encoding="utf-8")

    result = bootstrap_runtime_volume(runtime_root=runtime_root, template_dir=template)

    version_root = runtime_root / "data" / "business-agents" / "main-agent" / "version"
    assert (version_root / "worktrees" / "cs-old").is_dir()
    assert (version_root / "releases" / "release-old.tar.gz").read_text(encoding="utf-8") == "archive"
    assert result["migrated"]


@pytest.mark.parametrize("runtime_mode", ["container", "local-debug"])
def test_bootstrap_runtime_volume_copies_seed_bytes_without_rendering(tmp_path, runtime_mode):
    template = tmp_path / "template"
    workspace_template = template / "data" / "business-agents" / "response-disposal" / "workspace"
    settings_template = workspace_template / ".claude"
    settings_template.mkdir(parents=True)
    mcp_bytes = b'{"mcpServers":{"soc":{"type":"http","url":"${MCP_SERVER_URL}"}}}\n'
    settings_bytes = b'{"sandbox":{"network":{"allowedDomains":["localhost","*.internal"]}}}\n'
    binary_bytes = b"\x00\xffseed-bytes"
    (workspace_template / ".mcp.json").write_bytes(mcp_bytes)
    (settings_template / "settings.json").write_bytes(settings_bytes)
    (workspace_template / "asset.bin").write_bytes(binary_bytes)
    runtime_root = tmp_path / runtime_mode

    bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=template,
        runtime_volume_mode=runtime_mode,
        env={"MCP_SERVER_URL": "http://should-not-be-rendered.example/mcp"},
    )

    copied_workspace = runtime_root / "data" / "business-agents" / "response-disposal" / "workspace"
    assert (copied_workspace / ".mcp.json").read_bytes() == mcp_bytes
    assert (copied_workspace / ".claude" / "settings.json").read_bytes() == settings_bytes
    assert (copied_workspace / "asset.bin").read_bytes() == binary_bytes


def test_resolve_runtime_root_uses_local_debug_mode_default(tmp_path):
    env_file = tmp_path / ".env.local-debug"
    env_file.write_text("", encoding="utf-8")

    assert resolve_runtime_root(None, env_file) == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT
