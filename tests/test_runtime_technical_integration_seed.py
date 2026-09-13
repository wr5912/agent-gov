from __future__ import annotations

import io
import json
import tarfile

import yaml
from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware
from agentscope_runtime.subagent_templates import load_subagent_templates
from app.runtime_gateway.provisioning import agent_payload_from_workspace, session_settings_from_workspace
from scripts import runtime_technical_integration_seed as seed


def test_seed_package_is_a_real_minimal_agentscope_harness(tmp_path) -> None:
    agent_id = "runtime-technical-integration-package"
    package = seed.build_seed_package(agent_id)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        assert set(archive.getnames()) == {
            "workspace",
            "workspace/AGENT.md",
            "workspace/agent.yaml",
            "workspace/tests/README.md",
            "workspace/tests/test_runtime_harness.py",
        }
        assert all(member.isdir() or member.isfile() for member in archive.getmembers())
        for member in archive.getmembers():
            if member.isfile():
                content = archive.extractfile(member)
                assert content is not None
                (tmp_path / member.name.split("/")[-1]).write_bytes(content.read())
    manifest = yaml.safe_load((tmp_path / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["agent"]["id"] == agent_id
    assert manifest["agent"]["runtime_contract"] == AGENTSCOPE_RUNTIME_CONTRACT
    policy = manifest["workspace_policy"]
    assert policy["allowed_tools"] == policy["writable_paths"] == policy["allowed_network_domains"] == []
    assert policy["allow_for_run"] is False
    assert load_subagent_templates(tmp_path, "a" * 64) == {}
    AgentGovPolicyMiddleware(tmp_path)
    assert session_settings_from_workspace(tmp_path) == ("dont_ask", ".", "default")
    assert agent_payload_from_workspace(tmp_path, display_name="technical seed")["name"] == "technical seed"


def test_mcp_seed_package_is_exact_readonly_harness_with_url_reference_only(tmp_path) -> None:
    agent_id = seed.MCP_TECHNICAL_INTEGRATION_SCOPE
    package = seed.build_mcp_seed_package(agent_id)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        names = set(archive.getnames())
        assert names == {
            "workspace",
            "workspace/AGENT.md",
            "workspace/agent.yaml",
            "workspace/mcp/sec-ops.json",
            "workspace/tests/README.md",
            "workspace/tests/test_mcp_readonly_harness.py",
        }
        contents: dict[str, bytes] = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            content = archive.extractfile(member)
            assert content is not None
            contents[member.name] = content.read()

    manifest = yaml.safe_load(contents["workspace/agent.yaml"])
    declaration = json.loads(contents["workspace/mcp/sec-ops.json"])
    assert manifest["agent"]["id"] == agent_id
    assert manifest["workspace_policy"]["allowed_tools"] == list(seed.MCP_TECHNICAL_TOOL_NAMES)
    assert manifest["workspace_policy"]["allowed_network_domains"] == ["${SEC_OPS_MCP_URL}"]
    assert declaration == {
        "credential_refs": [{"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"}],
        "enable_resource_templates": [seed.MCP_TECHNICAL_RESOURCE_TEMPLATE],
        "enable_resources": [seed.MCP_TECHNICAL_RESOURCE_URI],
        "enable_tools": [seed.MCP_TECHNICAL_RAW_TOOL_NAME],
        "mcp_config": {"timeout": 30.0, "type": "http_mcp", "url": "${SEC_OPS_MCP_URL}"},
        "name": seed.MCP_TECHNICAL_SERVER_NAME,
        "schema_version": 1,
    }
    assert b"MCP_READONLY_ACCEPTANCE_COMPLETE" in contents["workspace/AGENT.md"]

    workspace = tmp_path / "workspace"
    (workspace / "mcp").mkdir(parents=True)
    (workspace / "AGENT.md").write_bytes(contents["workspace/AGENT.md"])
    (workspace / "agent.yaml").write_bytes(contents["workspace/agent.yaml"])
    (workspace / "mcp/sec-ops.json").write_bytes(contents["workspace/mcp/sec-ops.json"])
    AgentGovPolicyMiddleware(workspace, environ={"SEC_OPS_MCP_URL": "http://host.docker.internal:58001/mcp"})
