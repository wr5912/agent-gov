from __future__ import annotations

import json
from copy import deepcopy
from pathlib import PurePosixPath

import yaml
from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT
from jsonschema import Draft202012Validator, SchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from pydantic.types import JsonValue

from app.runtime.agent_workspace_package_schemas import NativeAgentDataInput
from app.runtime.business_agent_workspace import WorkspaceProvisionEntry
from app.runtime.json_types import JsonObject

NATIVE_AGENT_EDITABLE_FIELDS = frozenset(NativeAgentDataInput.model_fields)
_NATIVE_TEST_README_PATH = PurePosixPath("tests/README.md")
_NATIVE_TEST_PATH = PurePosixPath("tests/test_native_agent_harness_contract.py")


class NativeCandidateMappingError(ValueError):
    pass


def native_agent_data_entries(
    *,
    agent_id: str,
    agent_data: NativeAgentDataInput | JsonObject,
    schema: JsonObject,
    base_manifest: JsonObject | None = None,
) -> tuple[WorkspaceProvisionEntry, ...]:
    """Validate one native AgentData document and map it to governed Git files."""

    raw_agent_data = agent_data.model_dump(mode="json", exclude_unset=True) if isinstance(agent_data, NativeAgentDataInput) else agent_data
    _require_exact_schema_fields(raw_agent_data, schema)
    try:
        agent_data_record = NativeAgentDataInput.model_validate(raw_agent_data).model_dump(mode="json", exclude_unset=True)
    except PydanticValidationError as exc:
        raise NativeCandidateMappingError(f"AgentData validation failed: {exc.errors(include_url=False)[0]['msg']}") from exc
    _require_json_compatible(agent_data_record)
    _validate_native_schema(agent_data_record, schema)
    name = agent_data_record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise NativeCandidateMappingError("name must be a non-empty string")
    for field in ("context_config", "react_config"):
        if not isinstance(agent_data_record.get(field), dict):
            raise NativeCandidateMappingError(f"{field} must be an object")
    invite_config = agent_data_record.get("invite_config", {})
    if not isinstance(invite_config, dict):
        raise NativeCandidateMappingError("invite_config must be an object")
    if invite_config.get("invitable") is True and not str(invite_config.get("invite_description") or "").strip():
        raise NativeCandidateMappingError("invite_description is required when invitable is true")

    prompt = agent_data_record.get("system_prompt", _schema_default(schema, "system_prompt", "You're a helpful assistant."))
    if not isinstance(prompt, str):
        raise NativeCandidateMappingError("system_prompt must be a string")
    if not prompt.strip():
        raise NativeCandidateMappingError("system_prompt must be non-empty")

    manifest = _base_manifest(agent_id=agent_id, name=name.strip(), base_manifest=base_manifest)
    agent = manifest.get("agent")
    if not isinstance(agent, dict):
        raise NativeCandidateMappingError("base agent.yaml must contain an agent object")
    if agent.get("id") != agent_id:
        raise NativeCandidateMappingError("base agent.yaml identity does not match the target Agent")
    agent["name"] = name.strip()
    agent["system_prompt"] = "AGENT.md"
    manifest["context_config"] = deepcopy(agent_data_record["context_config"])
    manifest["react_config"] = deepcopy(agent_data_record["react_config"])
    manifest["invite_config"] = deepcopy(invite_config)

    manifest_text = yaml.safe_dump(
        manifest,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    entries = (
        WorkspaceProvisionEntry(PurePosixPath("agent.yaml"), manifest_text.encode("utf-8"), 0o644),
        WorkspaceProvisionEntry(PurePosixPath("AGENT.md"), prompt.encode("utf-8"), 0o644),
    )
    if base_manifest is not None:
        return entries
    return (*entries, *_native_workspace_test_entries(agent_id))


def native_agent_data_from_harness(
    *,
    agent_id: str,
    manifest: JsonObject,
    system_prompt: str,
    schema: JsonObject,
) -> NativeAgentDataInput:
    """Project only reviewed Agent-owned fields from one published Harness tree."""

    if manifest.get("schema_version") != 1:
        raise NativeCandidateMappingError("agent.yaml must use schema_version 1")
    agent = manifest.get("agent")
    if not isinstance(agent, dict) or agent.get("id") != agent_id:
        raise NativeCandidateMappingError("agent.yaml identity does not match the target Agent")
    if agent.get("system_prompt") != "AGENT.md":
        raise NativeCandidateMappingError("agent.yaml must bind the native system prompt to AGENT.md")
    name = agent.get("name")
    if not isinstance(name, str) or not name.strip():
        raise NativeCandidateMappingError("agent.yaml agent.name must be a non-empty string")
    agent_data_record: JsonObject = {
        "name": name,
        "system_prompt": system_prompt,
        "context_config": _native_config(manifest, "context_config"),
        "react_config": _native_config(manifest, "react_config"),
        "invite_config": _native_config(manifest, "invite_config"),
    }
    _require_exact_schema_fields(agent_data_record, schema)
    resolved_agent_data = _with_schema_defaults(agent_data_record, schema)
    _require_json_compatible(resolved_agent_data)
    _validate_native_schema(resolved_agent_data, schema)
    try:
        return NativeAgentDataInput.model_validate(resolved_agent_data)
    except PydanticValidationError as exc:
        message = exc.errors(include_url=False)[0]["msg"]
        raise NativeCandidateMappingError(f"Published AgentData projection is invalid: {message}") from exc


def _native_config(manifest: JsonObject, field: str) -> JsonObject:
    value = manifest.get(field)
    if not isinstance(value, dict):
        raise NativeCandidateMappingError(f"agent.yaml {field} must be an object")
    try:
        return TypeAdapter(JsonObject).validate_python(value)
    except PydanticValidationError as exc:
        raise NativeCandidateMappingError(f"agent.yaml {field} must contain JSON values") from exc


def _with_schema_defaults(value: JsonObject, schema: JsonObject) -> JsonObject:
    resolved = deepcopy(value)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise NativeCandidateMappingError("AgentScope AgentData schema has no properties")
    for field, field_schema in properties.items():
        if not isinstance(field, str) or not isinstance(field_schema, dict):
            raise NativeCandidateMappingError("AgentScope AgentData schema properties are invalid")
        current = resolved.get(field)
        if isinstance(current, dict):
            resolved[field] = _with_schema_defaults(current, field_schema)
        elif field not in resolved and "default" in field_schema:
            resolved[field] = deepcopy(field_schema["default"])
    return resolved


def _require_exact_schema_fields(agent_data_record: JsonObject, schema: JsonObject) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise NativeCandidateMappingError("AgentScope AgentData schema has no properties")
    schema_fields = set(properties)
    if schema_fields != NATIVE_AGENT_EDITABLE_FIELDS:
        raise NativeCandidateMappingError("AgentScope AgentData editable fields changed; mapping review is required")
    unexpected = set(agent_data_record) - NATIVE_AGENT_EDITABLE_FIELDS
    if unexpected:
        raise NativeCandidateMappingError(f"Unsupported AgentData fields: {', '.join(sorted(unexpected))}")
    _reject_unmapped_object_fields(agent_data_record, schema, path="AgentData")


def _reject_unmapped_object_fields(value: object, schema: object, *, path: str) -> None:
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        if value:
            raise NativeCandidateMappingError(f"{path} contains fields without a declared AgentScope schema")
        return
    unknown = set(value) - set(properties)
    if unknown:
        raise NativeCandidateMappingError(f"Unsupported {path} fields: {', '.join(sorted(unknown))}")
    for key, nested in value.items():
        _reject_unmapped_object_fields(nested, properties.get(key), path=f"{path}.{key}")


def _validate_native_schema(agent_data_record: JsonObject, schema: JsonObject) -> None:
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(agent_data_record)
    except SchemaError as exc:
        raise NativeCandidateMappingError("AgentScope AgentData schema is invalid") from exc
    except JsonSchemaValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "AgentData"
        raise NativeCandidateMappingError(f"AgentScope validation failed at {location}: {exc.message}") from exc


def _require_json_compatible(agent_data_record: JsonObject) -> None:
    try:
        json.dumps(agent_data_record, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise NativeCandidateMappingError("AgentData must contain finite JSON values") from exc


def _schema_default(schema: JsonObject, field: str, fallback: JsonValue) -> JsonValue:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return fallback
    field_schema = properties.get(field)
    if not isinstance(field_schema, dict):
        return fallback
    return deepcopy(field_schema.get("default", fallback))


def _base_manifest(
    *,
    agent_id: str,
    name: str,
    base_manifest: JsonObject | None,
) -> JsonObject:
    if base_manifest is not None:
        manifest = deepcopy(dict(base_manifest))
        if manifest.get("schema_version") != 1:
            raise NativeCandidateMappingError("base agent.yaml must use schema_version 1")
        return manifest
    return {
        "schema_version": 1,
        "agent": {
            "id": agent_id,
            "name": name,
            "version": "0.1.0",
            "language": "zh-CN",
            "runtime": "agentscope",
            "runtime_contract": AGENTSCOPE_RUNTIME_CONTRACT,
            "profile": agent_id,
            "system_prompt": "AGENT.md",
            "requires_web_hitl": False,
        },
        "session": {
            "model_profile": "default",
            "permission_mode": "default",
            "cwd": ".",
        },
        "workspace_policy": {
            "owner": "agentgov",
            "isolation": "per_agent",
            "immutable_harness": True,
            "fail_closed": True,
            "allow_for_run": False,
            "allowed_tools": [],
            "ask_tools": [],
            "denied_tools": ["Bash(*)", "Edit(/**)", "NotebookEdit(/**)", "Write(/**)"],
            "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
            "writable_paths": [],
            "denied_read_paths": ["**/*credential*", "**/*secret*", "**/.env", "**/.env.*", ".env", "/runtime-data/**"],
            "allowed_network_domains": [],
            "sandbox": {
                "enabled": True,
                "fail_if_unavailable": True,
                "allow_unsandboxed_commands": False,
            },
        },
        "runtime_middlewares": [
            {"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True},
            {"type": "system_prompt_context", "source": "AGENT.md"},
        ],
        "paths": {"workspace": "/workspace", "data_root": "/workspace/data"},
    }


def _native_workspace_test_entries(agent_id: str) -> tuple[WorkspaceProvisionEntry, WorkspaceProvisionEntry]:
    """为表单新建 Agent 生成可执行的真实 Harness 合约测试，不伪造运行回答。"""

    test_source = (
        "from pathlib import Path\n\n"
        "import yaml\n\n"
        "WORKSPACE = Path(__file__).resolve().parents[1]\n"
        f"EXPECTED_AGENT_ID = {agent_id!r}\n"
        f"EXPECTED_RUNTIME_CONTRACT = {AGENTSCOPE_RUNTIME_CONTRACT!r}\n\n\n"
        "def test_native_agent_harness_contract() -> None:\n"
        "    prompt = (WORKSPACE / 'AGENT.md').read_bytes()\n"
        "    manifest = yaml.safe_load((WORKSPACE / 'agent.yaml').read_text(encoding='utf-8'))\n"
        "    assert prompt.strip(), 'AGENT.md must contain the governed system prompt'\n"
        "    assert isinstance(manifest, dict) and manifest.get('schema_version') == 1\n"
        "    manifest_agent = manifest.get('agent')\n"
        "    assert isinstance(manifest_agent, dict)\n"
        "    assert manifest_agent.get('id') == EXPECTED_AGENT_ID\n"
        "    assert manifest_agent.get('profile') == EXPECTED_AGENT_ID\n"
        "    assert manifest_agent.get('runtime') == 'agentscope'\n"
        "    assert manifest_agent.get('runtime_contract') == EXPECTED_RUNTIME_CONTRACT\n"
        "    assert manifest_agent.get('system_prompt') == 'AGENT.md'\n"
        "    policy = manifest.get('workspace_policy')\n"
        "    assert isinstance(policy, dict)\n"
        "    assert policy.get('immutable_harness') is True\n"
        "    assert policy.get('fail_closed') is True\n"
        "    assert policy.get('allow_for_run') is False\n"
        "    for section in ('context_config', 'react_config', 'invite_config'):\n"
        "        assert isinstance(manifest.get(section), dict)\n"
    )
    readme = (
        "# Agent 测试套件\n\n"
        "`test_native_agent_harness_contract.py` 校验该候选的实际 `AGENT.md` 与 `agent.yaml`。"
        "发布前还应根据业务预期增加调用真实 Agent 的行为回归测试。\n"
    )
    return (
        WorkspaceProvisionEntry(_NATIVE_TEST_README_PATH, readme.encode("utf-8"), 0o644),
        WorkspaceProvisionEntry(_NATIVE_TEST_PATH, test_source.encode("utf-8"), 0o644),
    )
