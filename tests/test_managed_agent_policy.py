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
from app.runtime.protected_business_agents import SECURITY_OPERATIONS_EXPERT_AGENT_ID
from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "docker" / "runtime-bootstrap"


def _bootstrapped_runtime(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "runtime"
    bootstrap_runtime_volume(
        runtime_root=runtime_root,
        bootstrap_dir=BOOTSTRAP,
        runtime_volume_mode="local-debug",
        env={"SEC_OPS_MCP_URL": "http://unused.example/mcp", "SEC_OPS_MCP_TOKEN": "test-only"},
    )
    return runtime_root


def test_builtin_business_agent_workspace_matches_agentscope_policy(tmp_path: Path) -> None:
    runtime_root = _bootstrapped_runtime(tmp_path)
    workspace = runtime_root / "data" / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace"

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


def test_policy_is_structural_and_does_not_require_one_exact_tool_list(tmp_path: Path) -> None:
    runtime_root = _bootstrapped_runtime(tmp_path)
    workspace = runtime_root / "data" / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace"
    manifest = workspace / "agent.yaml"
    text = manifest.read_text(encoding="utf-8").replace("  - Bash(date *)\n", "")
    manifest.write_text(text, encoding="utf-8")

    assert plan_workspace_policy(workspace=workspace, agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID).violations == ()


def test_missing_required_prompt_is_rejected(tmp_path: Path) -> None:
    source = BOOTSTRAP / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(source, workspace)
    (workspace / "AGENT.md").unlink()

    violations = plan_workspace_policy(workspace=workspace, agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID).violations
    assert any(item.path == "AGENT.md" and item.rule_id == "required_asset_missing" for item in violations)


def test_symlinked_required_prompt_is_rejected_without_reading_external_content(tmp_path: Path) -> None:
    source = BOOTSTRAP / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(source, workspace)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (workspace / "AGENT.md").unlink()
    (workspace / "AGENT.md").symlink_to(outside)

    violations = plan_workspace_policy(workspace=workspace, agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID).violations
    assert any(item.path == "AGENT.md" and item.rule_id == "workspace_file_unreadable" for item in violations)
    assert outside.read_text(encoding="utf-8") == "secret\n"


@pytest.mark.parametrize(
    ("relative_path", "content", "rule_id"),
    [
        ("agent.yaml", "{", "invalid_manifest"),
        ("mcp/broken.json", "{", "invalid_mcp_json"),
    ],
)
def test_workspace_validator_rejects_invalid_structured_files(
    tmp_path: Path,
    relative_path: str,
    content: str,
    rule_id: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENT.md").write_text("prompt\n", encoding="utf-8")
    if relative_path != "agent.yaml":
        (workspace / "agent.yaml").write_text(
            "schema_version: 1\nagent: {id: custom-agent, runtime: agentscope, runtime_contract: agentscope-app/2.0.8}\n"
            "workspace_policy: {fail_closed: true, immutable_harness: true, allow_for_run: false}\n",
            encoding="utf-8",
        )
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    rules = {item.rule_id for item in plan_workspace_policy(workspace=workspace, agent_id="custom-agent").violations}
    assert rule_id in rules


def test_mcp_validator_accepts_http_with_exact_capability_allowlists(tmp_path: Path) -> None:
    config = {
        "schema_version": 1,
        "name": "remote",
        "credential_refs": [
            {"env": "REMOTE_MCP_URL", "path": "mcp_config.url"},
            {"env": "REMOTE_MCP_TOKEN", "path": "mcp_config.headers.Authorization"},
        ],
        "mcp_config": {
            "type": "http_mcp",
            "url": "${REMOTE_MCP_URL}",
            "headers": {"Authorization": "Bearer ${REMOTE_MCP_TOKEN}"},
        },
        "enable_tools": ["list_alerts"],
        "enable_resources": ["openapi://soc/alerts"],
        "enable_resource_templates": ["openapi://soc/alerts/{alert_id}"],
    }
    assert (
        validate_managed_mcp_content(
            json.dumps(config),
            agent_id="custom-agent",
            runtime_mode="local-debug",
            env={},
            runtime_root=tmp_path,
        )
        == ()
    )


def test_mcp_validator_rejects_stdio_and_wildcard_capabilities(tmp_path: Path) -> None:
    for config, rule_id in (
        (
            {
                "schema_version": 1,
                "name": "local",
                "credential_refs": [],
                "mcp_config": {"type": "stdio_mcp", "command": "python", "args": ["server.py"]},
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "invalid_mcp_config",
        ),
        (
            {
                "schema_version": 1,
                "name": "remote",
                "credential_refs": [],
                "mcp_config": {"type": "http_mcp", "url": "https://example.test/mcp"},
                "enable_tools": ["soc_api__*"],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "invalid_mcp_allowlist",
        ),
    ):
        violations = validate_managed_mcp_content(
            json.dumps(config),
            agent_id="custom-agent",
            runtime_mode="local-debug",
            env={},
            runtime_root=tmp_path,
        )
        assert [item.rule_id for item in violations] == [rule_id]


@pytest.mark.parametrize(
    ("config", "rule_id"),
    [
        (
            {
                "schema_version": 1,
                "name": "broken",
                "credential_refs": [],
                "mcp_config": {"type": "http_mcp", "url": "not-a-url"},
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "invalid_mcp_url",
        ),
        (
            {
                "schema_version": 1,
                "name": "ambiguous-endpoint",
                "credential_refs": [],
                "mcp_config": {"type": "http_mcp", "url": "https://example.test/mcp?tenant=other"},
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "invalid_mcp_url",
        ),
        (
            {
                "schema_version": 1,
                "name": "secret",
                "credential_refs": [{"env": "SECRET_MCP_URL", "path": "mcp_config.url"}],
                "mcp_config": {
                    "type": "http_mcp",
                    "url": "${SECRET_MCP_URL}",
                    "headers": {"Cookie": "session=live-secret"},
                },
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "inline_mcp_secret",
        ),
        (
            {
                "schema_version": 1,
                "name": "missing-ref",
                "credential_refs": [],
                "mcp_config": {"type": "http_mcp", "url": "${MISSING_REF_MCP_URL}"},
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "credential_ref_missing",
        ),
        (
            {
                "schema_version": 1,
                "name": "sec-ops",
                "credential_refs": [
                    {"env": "AGENTGOV_RUNTIME_SHARED_SECRET", "path": "mcp_config.url"},
                ],
                "mcp_config": {"type": "http_mcp", "url": "${AGENTGOV_RUNTIME_SHARED_SECRET}"},
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "invalid_credential_ref",
        ),
        (
            {
                "schema_version": 1,
                "name": "sec-ops",
                "credential_refs": [
                    {"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"},
                    {"env": "SEC_OPS_MCP_CLIENT_IP", "path": "mcp_config.headers.X-Forwarded-For"},
                ],
                "mcp_config": {
                    "type": "http_mcp",
                    "url": "${SEC_OPS_MCP_URL}",
                    "headers": {"X-Forwarded-For": "${SEC_OPS_MCP_CLIENT_IP}"},
                },
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            "invalid_mcp_header",
        ),
    ],
)
def test_mcp_validator_rejects_unsafe_configuration(tmp_path: Path, config: dict[str, object], rule_id: str) -> None:
    violations = validate_managed_mcp_content(
        json.dumps(config),
        agent_id="custom-agent",
        runtime_mode="local-debug",
        env={},
        runtime_root=tmp_path,
    )
    assert [item.rule_id for item in violations] == [rule_id]
