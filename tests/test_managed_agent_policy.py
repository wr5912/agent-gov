from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from app.runtime.managed_agent_policy import (
    plan_workspace_policy,
    runtime_workspace_policy_violations,
    validate_managed_mcp_content,
)
from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume

REPO_ROOT = Path(__file__).resolve().parents[1]
SEEDS = REPO_ROOT / "docker" / "runtime-volume-seeds"
SECURITY_OPERATIONS_EXPERT_AGENT_ID = "security-operations-expert"
SEEDED_AGENT_IDS = tuple(sorted(path.name for path in (SEEDS / "data" / "business-agents").iterdir() if (path / "workspace").is_dir()))


def _seeded_runtime(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "runtime"
    bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=SEEDS,
        runtime_volume_mode="local-debug",
        env={"MCP_SERVER_URL": "http://unused.example/mcp"},
    )
    return runtime_root


@pytest.mark.parametrize("agent_id", SEEDED_AGENT_IDS)
def test_seeded_business_agent_workspaces_match_managed_policy(tmp_path, agent_id):
    runtime_root = _seeded_runtime(tmp_path)
    workspace = runtime_root / "data" / "business-agents" / agent_id / "workspace"

    assert (
        runtime_workspace_policy_violations(
            workspace=workspace,
            agent_id=agent_id,
            runtime_mode="local-debug",
            env={},
            runtime_root=runtime_root,
        )
        == ()
    )


def test_security_operations_workspace_uses_generic_structural_managed_policy(tmp_path):
    runtime_root = _seeded_runtime(tmp_path)
    workspace = runtime_root / "data" / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace"
    settings_path = workspace / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["permissions"] = {"allow": ["Read(./**)"], "ask": [], "deny": []}
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    assert (
        runtime_workspace_policy_violations(
            workspace=workspace,
            agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
            runtime_mode="local-debug",
            env={},
            runtime_root=runtime_root,
        )
        == ()
    )


def test_missing_referenced_hook_is_rejected(tmp_path):
    template = SEEDS / "data" / "business-agents" / "main-agent" / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    (workspace / "hooks" / "pre_tool_guard.py").unlink()

    plan = plan_workspace_policy(workspace=workspace, agent_id="main-agent")

    assert any(item.path.endswith("hooks/pre_tool_guard.py") and item.rule_id == "referenced_hook_missing" for item in plan.violations)


def test_symlinked_hook_parent_is_rejected_without_reading_external_content(tmp_path):
    template = SEEDS / "data" / "business-agents" / "main-agent" / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    shutil.rmtree(workspace / "hooks")
    outside = tmp_path / "outside"
    outside.mkdir()
    external_hook = outside / "pre_tool_guard.py"
    external_hook.write_text("outside\n", encoding="utf-8")
    (workspace / "hooks").symlink_to(outside, target_is_directory=True)

    plan = plan_workspace_policy(workspace=workspace, agent_id="main-agent")

    assert any(item.path.endswith("hooks/pre_tool_guard.py") and item.rule_id == "unsafe_file_type" for item in plan.violations)
    assert external_hook.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize(
    ("relative_path", "content", "rule_id"),
    [
        (".claude/settings.json", "{", "invalid_settings"),
        (".mcp.json", "{", "invalid_mcp_json"),
    ],
)
def test_workspace_validator_rejects_invalid_json(tmp_path, relative_path, content, rule_id):
    workspace = tmp_path / "workspace"
    target = workspace / relative_path
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")

    plan = plan_workspace_policy(workspace=workspace, agent_id="custom-agent")

    assert {item.rule_id for item in plan.violations} == {rule_id}


def test_mcp_validator_accepts_real_urls_native_placeholders_headers_and_stdio(tmp_path):
    content = json.dumps(
        {
            "mcpServers": {
                "real": {
                    "type": "http",
                    "url": "https://user:secret@example.test/mcp",
                    "headers": {"Authorization": "Bearer live-value"},
                },
                "native-env": {"type": "http", "url": "${MCP_SERVER_URL}"},
                "native-default": {"type": "http", "url": "${MCP_SERVER_URL:-http://localhost:58001/mcp}"},
                "native-composite": {
                    "type": "http",
                    "url": "${API_BASE_URL:-https://api.example.com}/mcp",
                },
                "local": {"type": "stdio", "command": "python", "args": ["server.py"]},
            }
        }
    )

    assert (
        validate_managed_mcp_content(
            content,
            agent_id="custom-agent",
            runtime_mode="local-debug",
            env={},
            runtime_root=tmp_path,
        )
        == ()
    )


def test_mcp_validator_rejects_invalid_http_endpoint(tmp_path):
    content = json.dumps({"mcpServers": {"broken": {"type": "http", "url": "not-a-url"}}})

    violations = validate_managed_mcp_content(
        content,
        agent_id="custom-agent",
        runtime_mode="local-debug",
        env={},
        runtime_root=tmp_path,
    )

    assert [(item.rule_id, item.detail) for item in violations] == [("invalid_mcp_url", "broken")]
