from __future__ import annotations

import io
import json
import tarfile
from typing import Literal

import pytest
import yaml
from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware
from agentscope_runtime.subagent_templates import load_subagent_templates
from app.agent_testing.schemas import AgentTestRunItemResponse, AgentTestRunResponse
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
    test_source = (tmp_path / "test_runtime_harness.py").read_text(encoding="utf-8")
    assert "def test_minimal_runtime_harness_is_complete(agent):" in test_source
    assert "result = agent.run(" in test_source
    assert "assert not result.errors" in test_source
    assert "assert result.text.strip() == '14'" in test_source


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
    test_source = contents["workspace/tests/test_mcp_readonly_harness.py"].decode()
    assert "def test_mcp_readonly_harness_contract(agent):" in test_source
    assert "result = agent.run(" in test_source
    assert "assert not result.errors" in test_source
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


def _failed_test_record(**changes: object) -> AgentTestRunResponse:
    """失败记录仅验证日志投影，不代表真实 Agent 验收或写入 passed 证据。"""
    return AgentTestRunResponse.model_validate(
        {
            "test_run_id": "atr-diagnostic-contract",
            "agent_id": "technical-diagnostic",
            "change_set_id": "agc-diagnostic-contract",
            "commit_sha": "a" * 40,
            "source": "release_check",
            "status": "failed",
            "created_at": "2026-01-01T00:00:00Z",
            "exit_code": 1,
            "suite_digest": "b" * 64,
            **changes,
        }
    )


def _failure_output(record: AgentTestRunResponse) -> str:
    encoded = seed._candidate_test_failure(record, agent_id="technical-diagnostic", change_set_id="agc-diagnostic-contract", commit_sha="a" * 40)
    return encoded.split(": ", 1)[1]


@pytest.mark.parametrize("status", ["queued", "running", "failed", "error", "cancelled"])
def test_candidate_failure_distinguishes_pending_timeout_and_rejected_terminal(status: str) -> None:
    record = _failed_test_record(status=status, exit_code=None if status in {"queued", "running"} else 1)
    summary = json.loads(_failure_output(record))
    assert summary == {
        "status": status,
        "failure_reason": "pending_timeout" if status in {"queued", "running"} else "gate_rejected",
        "exit_code": record.exit_code,
        "suite_digest_present": True,
        "agent_match": True,
        "change_set_match": True,
        "commit_match": True,
        "error_code": "unclassified",
        "http_status": None,
        "http_error_code": None,
        "first_nonpass_item": None,
    }


@pytest.mark.parametrize(
    ("detail", "failure_kind"),
    [
        ("E   AssertionError: withheld-content", "AssertionError"),
        ("E   assert None == []", "AssertionError"),
        ("E   httpx.HTTPStatusError: withheld-content", "HTTPStatusError"),
        ("E   RuntimeError: withheld-content", "RuntimeError"),
        ("withheld-content mentions HTTPStatusError", "other"),
    ],
)
def test_candidate_failure_reports_fixed_pytest_kind_without_raw_content(detail: str, failure_kind: str) -> None:
    nodeid = "tests/test_runtime_harness.py::test_minimal_runtime_harness_is_complete"
    item = AgentTestRunItemResponse(nodeid=nodeid, outcome="failed", phase="call", detail=detail)
    record = _failed_test_record(items=[item], error={"error_code": "AGENT_TEST_REPORT_INVALID", "message": "withheld-content"})
    summary = json.loads(_failure_output(record))
    assert summary["first_nonpass_item"] == {"nodeid": nodeid, "outcome": "failed", "phase": "call", "failure_kind": failure_kind}
    assert summary["error_code"] == "AGENT_TEST_REPORT_INVALID"
    assert "withheld-content" not in json.dumps(summary)


def test_candidate_failure_projects_only_safe_metadata_and_exact_identity_comparisons() -> None:
    private = "withheld-content"
    record = _failed_test_record(
        agent_id=private,
        change_set_id=private,
        commit_sha=private,
        suite_digest=None,
        stdout=private,
        stderr=private,
        report={"response": private},
        error={"error_code": "BAD_CODE\n" + private},
        items=[{"nodeid": "test_case[" + private + "]", "outcome": private, "phase": private, "detail": private}],
    )
    summary = json.loads(_failure_output(record))
    assert summary["agent_match"] is summary["change_set_match"] is summary["commit_match"] is summary["suite_digest_present"] is False
    assert summary["error_code"] == "unclassified"
    assert summary["first_nonpass_item"] == {"nodeid": "unclassified", "outcome": "unclassified", "phase": "unclassified", "failure_kind": "other"}
    assert private not in json.dumps(summary)


def test_candidate_failure_projects_only_http_status_and_validated_error_code() -> None:
    detail = (
        "E   agentgov_testkit._transport.AgentGovTestkitError: "
        "AgentGov test invocation failed: HTTP 409 error_code=RUNTIME_STATE_CONFLICT\n"
        "private response and URL must not appear"
    )
    item = AgentTestRunItemResponse(
        nodeid="tests/test_runtime_harness.py::test_minimal_runtime_harness_is_complete",
        outcome="failed",
        phase="call",
        detail=detail,
    )
    summary = json.loads(_failure_output(_failed_test_record(items=[item])))
    assert summary["http_status"] == 409
    assert summary["http_error_code"] == "RUNTIME_STATE_CONFLICT"
    assert "private response" not in json.dumps(summary)


@pytest.mark.parametrize("stage", ["abandon", "delete"])
def test_cleanup_failure_preserves_primary_error_and_reports_only_stage(stage: Literal["abandon", "delete"], capsys) -> None:
    primary = seed.TechnicalIntegrationSeedError("primary failure")
    with pytest.raises(seed.TechnicalIntegrationSeedError) as caught:
        try:
            raise primary
        finally:
            seed._cleanup_failure(stage, RuntimeError("withheld-content"), primary_failed=True)
    assert caught.value is primary
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"stage={stage}; error_type=RuntimeError" in captured.err
    assert "withheld-content" not in captured.err
    with pytest.raises(seed.TechnicalIntegrationSeedError, match=f"stage={stage}") as cleanup:
        seed._cleanup_failure(stage, RuntimeError("withheld-content"), primary_failed=False)
    assert "withheld-content" not in str(cleanup.value)
