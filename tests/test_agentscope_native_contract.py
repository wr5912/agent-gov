from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from agentscope_runtime._generated_operation_policy import POLICY_SHA256
from agentscope_runtime.access_middleware import FixedRuntimeUserMiddleware
from scripts.agentscope_native_contract import (
    ALLOWLIST_DECISIONS,
    EXPECTED_AGENTSCOPE_VERSION,
    EXPECTED_OPERATION_COUNT,
    EXPECTED_PATH_COUNT,
    OPENAPI_PATH,
    POLICY_PATH,
    RUNTIME_POLICY_PATH,
    audit_contract,
    audit_operation_policy,
    build_agentscope_openapi,
    load_json_object,
    operation_items,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_json_object(POLICY_PATH)
OPERATIONS = POLICY["operations"]
assert isinstance(OPERATIONS, list)


def _operation_test_id(value: object) -> str:
    return str(value["test_id"]) if isinstance(value, dict) else "invalid-operation"


def _concrete_path(template: str) -> str:
    return re.sub(r"\{[^{}]+\}", "contract-id", template)


def test_fixed_public_create_app_export_matches_snapshot_and_generated_policy() -> None:
    fresh = build_agentscope_openapi()
    snapshot = load_json_object(OPENAPI_PATH)

    assert fresh == snapshot
    assert len(snapshot["paths"]) == EXPECTED_PATH_COUNT
    assert len(operation_items(snapshot)) == EXPECTED_OPERATION_COUNT
    assert (
        audit_contract(
            fresh_schema=fresh,
            snapshot=snapshot,
            policy=POLICY,
            runtime_policy_text=RUNTIME_POLICY_PATH.read_text(encoding="utf-8"),
        )
        == []
    )
    assert hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest() == POLICY_SHA256


def test_supplemental_types_are_derived_from_fixed_public_models() -> None:
    schema = load_json_object(OPENAPI_PATH)
    metadata = schema["x-agentgov-native-contract"]
    schemas = schema["components"]["schemas"]
    message_items = schemas["ListMessagesResponse"]["properties"]["messages"]["items"]
    event_schema = schema["paths"]["/sessions/{session_id}/stream"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert metadata == {
        "distribution": "agentscope",
        "factory": "agentscope.app.create_app",
        "public_model_sources": {
            "AgentScopeAgentEvent": "agentscope.event.AgentEvent",
            "AgentScopeMsg": "agentscope.message.Msg",
        },
        "version": EXPECTED_AGENTSCOPE_VERSION,
    }
    assert message_items == {"$ref": "#/components/schemas/AgentScopeMsg"}
    assert event_schema == {"$ref": "#/components/schemas/AgentScopeAgentEvent"}
    assert {"id", "created_at", "metadata"} <= set(schemas["AgentScopeMsg"]["required"])
    for name in ("AgentScopeReplyStartEvent", "AgentScopeTextBlockDeltaEvent", "AgentScopeCustomEvent"):
        assert {"id", "created_at", "metadata", "type"} <= set(schemas[name]["required"])


@pytest.mark.parametrize("entry", OPERATIONS, ids=_operation_test_id)
def test_each_operation_policy_entry_drives_runtime_access(entry: object) -> None:
    assert isinstance(entry, dict)
    active_conditions = set(POLICY["active_enable_conditions"])
    expected = entry["decision"] in ALLOWLIST_DECISIONS and entry["enable_condition"] in active_conditions

    assert (
        FixedRuntimeUserMiddleware._is_allowed(
            str(entry["method"]),
            _concrete_path(str(entry["path"])),
        )
        is expected
    )


def test_agent_schema_v2_and_governed_workspace_reads_are_the_only_new_active_surfaces() -> None:
    is_allowed = FixedRuntimeUserMiddleware._is_allowed

    assert is_allowed("GET", "/agent/schema/v2")
    assert not is_allowed("GET", "/agent/schema")
    assert is_allowed("PATCH", "/sessions/session-1")
    assert is_allowed("GET", "/workspace/status")
    assert is_allowed("GET", "/workspace/mcp")
    assert is_allowed("GET", "/workspace/skill")
    assert not is_allowed("GET", "/workspace/files")
    assert not is_allowed("GET", "/workspace/directories")
    assert not is_allowed("POST", "/workspace/files/download-token")
    assert not is_allowed("POST", "/workspace/mcp")
    assert not is_allowed("POST", "/workspace/skill")


def test_policy_completeness_and_generated_artifact_drift_fail_closed() -> None:
    schema = load_json_object(OPENAPI_PATH)
    missing = copy.deepcopy(POLICY)
    missing_operations = missing["operations"]
    assert isinstance(missing_operations, list)
    removed = missing_operations.pop()
    assert isinstance(removed, dict)

    issues = audit_operation_policy(schema, missing)
    assert f"operation policy missing {removed['method']} {removed['path']}" in issues

    drift_issues = audit_contract(
        fresh_schema=schema,
        snapshot=schema,
        policy=POLICY,
        runtime_policy_text=RUNTIME_POLICY_PATH.read_text(encoding="utf-8") + "# drift\n",
    )
    assert "agentscope_runtime/_generated_operation_policy.py differs from the operation policy" in drift_issues


def test_non_exposed_decision_cannot_be_activated_by_condition_only() -> None:
    schema = load_json_object(OPENAPI_PATH)
    policy = copy.deepcopy(POLICY)
    operations = policy["operations"]
    assert isinstance(operations, list)
    candidate = next(entry for entry in operations if isinstance(entry, dict) and entry["test_id"] == "candidate-only:POST:/workspace/mcp")
    candidate["enable_condition"] = "runtime-core"

    issues = audit_operation_policy(schema, policy)

    assert any("decision 'candidate-only' cannot use active condition 'runtime-core'" in issue for issue in issues)


def test_frontend_native_runtime_types_do_not_redeclare_agentscope_dtos() -> None:
    source = (ROOT / "frontend" / "src" / "types" / "runtime.ts").read_text(encoding="utf-8")

    assert "components as AgentScopeComponents" in source
    assert 'AgentScopeComponents["schemas"]["AgentScopeAgentEvent"]' in source
    assert 'AgentScopeComponents["schemas"]["AgentScopeMsg"]' in source
    assert 'AgentScopeComponents["schemas"]["ChatRequest"]["input"]' in source
    assert "export type GovernedRuntimeSessionView = AgentScopeSessionView &" in source
    assert "export type AgentScopeChatReceipt = AgentScopeChatResponse &" in source
    assert not re.search(r"export interface AgentScope(?:Session|Message|AgentEvent)", source)


def test_policy_json_is_stable_and_machine_readable() -> None:
    reparsed = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    assert reparsed == POLICY
    assert audit_operation_policy(load_json_object(OPENAPI_PATH), reparsed) == []
