#!/usr/bin/env python3
"""导出并校验固定 AgentScope 公共 API、补充模型与访问策略。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from importlib.metadata import version
from pathlib import Path

from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.event import AgentEvent, EventBase
from agentscope.message import Msg
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_AGENTSCOPE_VERSION = "2.0.8"
EXPECTED_PATH_COUNT = 67
EXPECTED_OPERATION_COUNT = 86
OPENAPI_PATH = ROOT / "config" / "agentscope_openapi.json"
POLICY_PATH = ROOT / "config" / "agentscope_operation_policy.json"
RUNTIME_POLICY_PATH = ROOT / "agentscope_runtime" / "_generated_operation_policy.py"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
DECISIONS = frozenset({"owner-facing", "internal", "candidate-only", "disabled", "deprecated"})
ALLOWLIST_DECISIONS = frozenset({"owner-facing", "internal"})
PUBLIC_MODEL_SOURCES = {
    "AgentScopeAgentEvent": "agentscope.event.AgentEvent",
    "AgentScopeMsg": "agentscope.message.Msg",
}
_TEMPLATE_PARAMETER = re.compile(r"\{[^{}]+\}")

JsonObject = dict[str, object]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="重建 OpenAPI 与 Runtime policy 派生产物")
    action.add_argument("--check", action="store_true", help="检查版本、快照、策略和派生产物漂移")
    args = parser.parse_args()

    fresh_schema = build_agentscope_openapi()
    policy = load_json_object(POLICY_PATH)
    if args.write:
        issues = audit_operation_policy(fresh_schema, policy)
        if issues:
            _print_issues(issues)
            return 1
        OPENAPI_PATH.write_text(_json_text(fresh_schema), encoding="utf-8")
        RUNTIME_POLICY_PATH.write_text(render_runtime_policy(policy), encoding="utf-8")
        print(
            "AgentScope native contract written: "
            f"version={EXPECTED_AGENTSCOPE_VERSION} paths={len(_paths(fresh_schema))} "
            f"operations={len(operation_items(fresh_schema))}",
        )
        return 0

    snapshot = load_json_object(OPENAPI_PATH)
    issues = audit_contract(
        fresh_schema=fresh_schema,
        snapshot=snapshot,
        policy=policy,
        runtime_policy_text=RUNTIME_POLICY_PATH.read_text(encoding="utf-8"),
    )
    if issues:
        _print_issues(issues)
        return 1
    print(
        f"AgentScope native contract OK: version={EXPECTED_AGENTSCOPE_VERSION} paths={len(_paths(snapshot))} operations={len(operation_items(snapshot))}",
    )
    return 0


def build_agentscope_openapi() -> JsonObject:
    installed = version("agentscope")
    if installed != EXPECTED_AGENTSCOPE_VERSION:
        raise RuntimeError(
            f"agentscope version {installed!r} != fixed {EXPECTED_AGENTSCOPE_VERSION!r}",
        )
    app = create_app(
        storage=AsyncSQLAlchemyStorage("sqlite+aiosqlite:///:memory:"),
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager("/tmp/agentgov-agentscope-contract"),
        knowledge_base_manager=None,
        enable_index_worker=False,
        enable_channel_worker=False,
        enable_scheduler=False,
        channels=[],
        mcp_hubs=[],
        skill_hubs=[],
        title="AgentScope",
        version=EXPECTED_AGENTSCOPE_VERSION,
    )
    raw_schema = copy.deepcopy(app.openapi())
    if len(_paths(raw_schema)) != EXPECTED_PATH_COUNT:
        raise RuntimeError(
            f"AgentScope public create_app paths={len(_paths(raw_schema))}, expected {EXPECTED_PATH_COUNT}",
        )
    if len(operation_items(raw_schema)) != EXPECTED_OPERATION_COUNT:
        raise RuntimeError(
            f"AgentScope public create_app operations={len(operation_items(raw_schema))}, expected {EXPECTED_OPERATION_COUNT}",
        )
    return _add_public_model_schemas(raw_schema)


def audit_contract(
    *,
    fresh_schema: JsonObject,
    snapshot: JsonObject,
    policy: JsonObject,
    runtime_policy_text: str,
) -> list[str]:
    issues = audit_operation_policy(fresh_schema, policy)
    if snapshot != fresh_schema:
        issues.append("config/agentscope_openapi.json differs from the fixed public create_app export")
    expected_runtime_policy = render_runtime_policy(policy)
    if runtime_policy_text != expected_runtime_policy:
        issues.append("agentscope_runtime/_generated_operation_policy.py differs from the operation policy")
    issues.extend(_audit_supplemental_contract(snapshot))
    return issues


def audit_operation_policy(schema: JsonObject, policy: JsonObject) -> list[str]:
    issues: list[str] = []
    source = policy.get("source")
    expected_source = {
        "distribution": "agentscope",
        "version": EXPECTED_AGENTSCOPE_VERSION,
        "factory": "agentscope.app.create_app",
        "openapi_snapshot": "config/agentscope_openapi.json",
    }
    if source != expected_source:
        issues.append(f"operation policy source {source!r} != {expected_source!r}")
    active_conditions = _string_set(policy.get("active_enable_conditions"))
    if not active_conditions:
        issues.append("operation policy active_enable_conditions must be a non-empty string list")

    raw_entries = policy.get("operations")
    if not isinstance(raw_entries, list):
        return [*issues, "operation policy operations must be a list"]
    actual: dict[tuple[str, str], JsonObject] = {}
    test_ids: set[str] = set()
    for index, value in enumerate(raw_entries):
        if not isinstance(value, dict):
            issues.append(f"operation policy entry {index} must be an object")
            continue
        entry = value
        method = entry.get("method")
        path = entry.get("path")
        key = (str(path), str(method).lower())
        if key in actual:
            issues.append(f"duplicate operation policy entry {method} {path}")
            continue
        actual[key] = entry
        issues.extend(_audit_policy_entry(index, entry, active_conditions, test_ids))

    expected = {(path, method): operation for path, method, operation in operation_items(schema)}
    for path, method in sorted(expected.keys() - actual.keys()):
        issues.append(f"operation policy missing {method.upper()} {path}")
    for path, method in sorted(actual.keys() - expected.keys()):
        issues.append(f"operation policy contains unknown {method.upper()} {path}")
    for key in sorted(expected.keys() & actual.keys()):
        operation_id = expected[key].get("operationId")
        if actual[key].get("operation_id") != operation_id:
            issues.append(
                f"operation policy {key[1].upper()} {key[0]} operation_id {actual[key].get('operation_id')!r} != {operation_id!r}",
            )
        expected_family = operation_family(key[0])
        if actual[key].get("family") != expected_family:
            issues.append(
                f"operation policy {key[1].upper()} {key[0]} family {actual[key].get('family')!r} != {expected_family!r}",
            )
    return issues


def render_runtime_policy(policy: JsonObject) -> str:
    active_conditions = _string_set(policy.get("active_enable_conditions"))
    entries = policy.get("operations")
    if not isinstance(entries, list):
        raise ValueError("operation policy operations must be a list")
    allowed: list[tuple[str, str]] = []
    for value in entries:
        if not isinstance(value, dict):
            raise ValueError("operation policy entry must be an object")
        if value.get("decision") not in ALLOWLIST_DECISIONS:
            continue
        if value.get("enable_condition") not in active_conditions:
            continue
        method, path = value.get("method"), value.get("path")
        if not isinstance(method, str) or not isinstance(path, str):
            raise ValueError("allowlisted operation must have string method/path")
        allowed.append((method.upper(), path))
    allowed.sort()
    digest = hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()
    rows = "\n".join(f"    ({json.dumps(method)}, {json.dumps(path)})," for method, path in allowed)
    return (
        '"""This file was auto-generated by scripts/agentscope_native_contract.py."""\n\n'
        f"POLICY_SHA256 = {json.dumps(digest)}\n"
        "ALLOWED_RUNTIME_OPERATIONS: tuple[tuple[str, str], ...] = (\n"
        f"{rows}\n"
        ")\n"
    )


def operation_items(schema: JsonObject) -> list[tuple[str, str, JsonObject]]:
    items: list[tuple[str, str, JsonObject]] = []
    for path, path_item in _paths(schema).items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method in HTTP_METHODS and isinstance(operation, dict):
                items.append((path, method, operation))
    return items


def operation_family(path: str) -> str:
    segment = path.lstrip("/").split("/", 1)[0]
    return {
        "agent": "agent",
        "chat": "chat",
        "channels": "channels",
        "credential": "credentials",
        "embedding-model": "models",
        "health": "health",
        "hub": "hub",
        "knowledge_bases": "knowledge",
        "mcp": "mcp-library",
        "model": "models",
        "schedule": "schedule",
        "sessions": "session",
        "skill": "skill-library",
        "tts-model": "models",
        "workspace": "workspace",
    }.get(segment, "unknown")


def load_json_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _add_public_model_schemas(schema: JsonObject) -> JsonObject:
    components = schema.setdefault("components", {})
    if not isinstance(components, dict):
        raise RuntimeError("AgentScope OpenAPI components must be an object")
    schemas = components.setdefault("schemas", {})
    if not isinstance(schemas, dict):
        raise RuntimeError("AgentScope OpenAPI components.schemas must be an object")

    event_schema = TypeAdapter(AgentEvent).json_schema(mode="serialization")
    _require_serialized_defaults(event_schema, EventBase, union_definitions=True)
    _require_const_discriminators(event_schema, union_definitions=True)
    _merge_public_model_schema(schemas, "AgentScopeAgentEvent", event_schema)
    message_schema = Msg.model_json_schema(mode="serialization")
    _require_serialized_defaults(message_schema, Msg)
    _require_const_discriminators(message_schema)
    _merge_public_model_schema(schemas, "AgentScopeMsg", message_schema)
    list_messages = schemas.get("ListMessagesResponse")
    messages = _nested_object(list_messages, "properties", "messages")
    messages["items"] = {"$ref": "#/components/schemas/AgentScopeMsg"}

    stream = _operation(schema, "/sessions/{session_id}/stream", "get")
    response = _nested_object(stream, "responses", "200", "content", "application/json")
    response["schema"] = {"$ref": "#/components/schemas/AgentScopeAgentEvent"}
    schema["x-agentgov-native-contract"] = {
        "distribution": "agentscope",
        "version": EXPECTED_AGENTSCOPE_VERSION,
        "factory": "agentscope.app.create_app",
        "public_model_sources": PUBLIC_MODEL_SOURCES,
    }
    return schema


def _merge_public_model_schema(schemas: JsonObject, root_name: str, model_schema: JsonObject) -> None:
    definitions = model_schema.pop("$defs", {})
    if not isinstance(definitions, dict):
        raise RuntimeError(f"{root_name} $defs must be an object")
    names = {str(name): f"AgentScope{name}" for name in definitions}
    names[root_name.removeprefix("AgentScope")] = root_name
    root = _rewrite_schema_refs(model_schema, names)
    _put_unique_schema(schemas, root_name, root)
    for name, definition in definitions.items():
        if not isinstance(name, str) or not isinstance(definition, dict):
            raise RuntimeError(f"{root_name} definition must be an object")
        _put_unique_schema(schemas, names[name], _rewrite_schema_refs(definition, names))


def _require_serialized_defaults(
    model_schema: JsonObject,
    model: type[EventBase] | type[Msg],
    *,
    union_definitions: bool = False,
) -> None:
    """Reflect fields emitted by public ``model_dump(mode='json')`` calls."""
    default_factory_fields = {name for name, field in model.model_fields.items() if field.default_factory is not None}
    _require_schema_properties(model_schema, default_factory_fields, union_definitions=union_definitions)


def _require_const_discriminators(
    model_schema: JsonObject,
    *,
    union_definitions: bool = False,
) -> None:
    """Pydantic defaulted Literal discriminators are present on serialized output."""
    for schema in _schema_objects(model_schema, union_definitions=union_definitions):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        discriminator_fields = {name for name, value in properties.items() if isinstance(name, str) and isinstance(value, dict) and "const" in value}
        _add_required_fields(schema, discriminator_fields)


def _require_schema_properties(
    model_schema: JsonObject,
    field_names: set[str],
    *,
    union_definitions: bool = False,
) -> None:
    for schema in _schema_objects(model_schema, union_definitions=union_definitions):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        _add_required_fields(schema, field_names & set(properties))


def _schema_objects(model_schema: JsonObject, *, union_definitions: bool) -> list[JsonObject]:
    if not union_definitions:
        return [model_schema]
    definitions = model_schema.get("$defs")
    alternatives = model_schema.get("anyOf")
    if not isinstance(definitions, dict) or not isinstance(alternatives, list):
        return []
    names = {
        value["$ref"].removeprefix("#/$defs/")
        for value in alternatives
        if isinstance(value, dict) and isinstance(value.get("$ref"), str) and str(value["$ref"]).startswith("#/$defs/")
    }
    return [value for name, value in definitions.items() if name in names and isinstance(value, dict)]


def _add_required_fields(schema: JsonObject, field_names: set[str]) -> None:
    if not field_names:
        return
    required = schema.get("required")
    required_fields = {value for value in required if isinstance(value, str)} if isinstance(required, list) else set()
    schema["required"] = sorted(required_fields | field_names)


def _put_unique_schema(schemas: JsonObject, name: str, schema: JsonObject) -> None:
    existing = schemas.get(name)
    if existing is not None and existing != schema:
        raise RuntimeError(f"supplemental public model schema collision: {name}")
    schemas[name] = schema


def _rewrite_schema_refs(value: object, names: dict[str, str]) -> object:
    if isinstance(value, list):
        return [_rewrite_schema_refs(item, names) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten: JsonObject = {}
    for key, item in value.items():
        if key == "$ref" and isinstance(item, str) and item.startswith("#/$defs/"):
            source_name = item.removeprefix("#/$defs/")
            rewritten[key] = f"#/components/schemas/{names[source_name]}"
        else:
            rewritten[key] = _rewrite_schema_refs(item, names)
    return rewritten


def _audit_policy_entry(
    index: int,
    entry: JsonObject,
    active_conditions: set[str],
    test_ids: set[str],
) -> list[str]:
    issues: list[str] = []
    required = {
        "method",
        "path",
        "operation_id",
        "family",
        "decision",
        "owner",
        "enable_condition",
        "test_id",
    }
    if set(entry) != required:
        issues.append(
            f"operation policy entry {index} fields {sorted(entry)} != {sorted(required)}",
        )
    for field in required:
        if not isinstance(entry.get(field), str) or not str(entry[field]).strip():
            issues.append(f"operation policy entry {index} {field} must be a non-empty string")
    method = entry.get("method")
    if isinstance(method, str) and (method != method.upper() or method.lower() not in HTTP_METHODS):
        issues.append(f"operation policy entry {index} method {method!r} is invalid")
    path = entry.get("path")
    if isinstance(path, str):
        static_path = _TEMPLATE_PARAMETER.sub("id", path)
        if not path.startswith("/") or "{" in static_path or "}" in static_path:
            issues.append(f"operation policy entry {index} path {path!r} is invalid")
    decision = entry.get("decision")
    if decision not in DECISIONS:
        issues.append(f"operation policy entry {index} decision {decision!r} is invalid")
    condition = entry.get("enable_condition")
    if decision not in ALLOWLIST_DECISIONS and condition in active_conditions:
        issues.append(
            f"operation policy entry {index} decision {decision!r} cannot use active condition {condition!r}",
        )
    test_id = entry.get("test_id")
    if isinstance(test_id, str):
        if test_id in test_ids:
            issues.append(f"operation policy entry {index} duplicate test_id {test_id!r}")
        test_ids.add(test_id)
    return issues


def _audit_supplemental_contract(schema: JsonObject) -> list[str]:
    issues: list[str] = []
    schemas = _nested_object(schema, "components", "schemas")
    list_messages = schemas.get("ListMessagesResponse")
    try:
        message_items = _nested_object(list_messages, "properties", "messages", "items")
    except RuntimeError as exc:
        issues.append(str(exc))
    else:
        if message_items != {"$ref": "#/components/schemas/AgentScopeMsg"}:
            issues.append("ListMessagesResponse.messages.items is not derived from agentscope.message.Msg")
    stream = _operation(schema, "/sessions/{session_id}/stream", "get")
    try:
        event_schema = _nested_object(
            stream,
            "responses",
            "200",
            "content",
            "application/json",
            "schema",
        )
    except RuntimeError as exc:
        issues.append(str(exc))
    else:
        if event_schema != {"$ref": "#/components/schemas/AgentScopeAgentEvent"}:
            issues.append("Session SSE schema is not derived from agentscope.event.AgentEvent")
    return issues


def _paths(schema: JsonObject) -> JsonObject:
    paths = schema.get("paths")
    return paths if isinstance(paths, dict) else {}


def _operation(schema: JsonObject, path: str, method: str) -> JsonObject:
    path_item = _paths(schema).get(path)
    operation = path_item.get(method) if isinstance(path_item, dict) else None
    if not isinstance(operation, dict):
        raise RuntimeError(f"missing AgentScope operation {method.upper()} {path}")
    return operation


def _nested_object(value: object, *keys: str) -> JsonObject:
    current = value
    pointer = ""
    for key in keys:
        pointer += f"/{key}"
        current = current.get(key) if isinstance(current, dict) else None
        if not isinstance(current, dict):
            raise RuntimeError(f"expected object at {pointer}")
    return current


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return set()
    return set(value)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _print_issues(issues: list[str]) -> None:
    for issue in issues:
        print(f"AGENTSCOPE_NATIVE_CONTRACT_FAIL: {issue}")


if __name__ == "__main__":
    raise SystemExit(main())
