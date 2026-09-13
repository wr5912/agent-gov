"""通过治理 API 暴露固定版 AgentScope Agent 表单 schema。"""

from __future__ import annotations

from copy import deepcopy

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.runtime.agent_workspace_package_schemas import (
    NativeAgentDataInput,
    NativeContextConfig,
    NativeInviteConfig,
    NativeReactConfig,
)
from app.runtime.json_types import JsonObject

from ._router_operations import _call
from .client import AgentScopeRuntimeClient, RuntimeUpstreamError

_SUPPORTED_AGENT_FIELDS = frozenset(NativeAgentDataInput.model_fields)
_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "context_config": NativeContextConfig,
    "react_config": NativeReactConfig,
    "invite_config": NativeInviteConfig,
}
_ROOT_SCHEMA_KEYS = frozenset({"type", "title", "description", "required", "properties"})
_OBJECT_SCHEMA_KEYS = frozenset({"type", "title", "description", "properties"})
_SCALAR_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "anyOf",
    },
)
_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})


class RuntimeNativeAgentSchemaResponse(BaseModel):
    """仅承载通过安全投影校验的 AgentScope JSON Schema。"""

    model_config = ConfigDict(populate_by_name=True)

    schema_: JsonObject = Field(alias="schema", serialization_alias="schema")


def register_native_agent_schema_route(
    router: APIRouter,
    *,
    client: AgentScopeRuntimeClient,
) -> None:
    @router.get(
        "/agent-schema",
        response_model=RuntimeNativeAgentSchemaResponse,
        summary="Read the pinned AgentScope AgentData form schema",
    )
    async def native_agent_schema() -> RuntimeNativeAgentSchemaResponse:
        upstream = await _call(client, "GET", "/agent/schema/v2")
        return _project_native_agent_schema(upstream.body)


def _project_native_agent_schema(body: object) -> RuntimeNativeAgentSchemaResponse:
    if not isinstance(body, dict) or set(body) != {"schema"}:
        raise _invalid_schema_response()
    schema = body.get("schema")
    if not isinstance(schema, dict):
        raise _invalid_schema_response()
    _validate_form_schema(schema)
    return RuntimeNativeAgentSchemaResponse.model_validate({"schema": deepcopy(schema)})


def validate_native_agent_form_schema(body: object) -> JsonObject:
    """返回供候选写链复用的同一安全表单 schema。"""

    return _project_native_agent_schema(body).schema_


def _validate_form_schema(schema: dict[object, object]) -> None:
    if set(schema) != _ROOT_SCHEMA_KEYS or schema.get("type") != "object":
        raise _invalid_schema_response()
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != _SUPPORTED_AGENT_FIELDS:
        raise _invalid_schema_response()
    required = schema.get("required")
    expected_required = {name for name, field in NativeAgentDataInput.model_fields.items() if field.is_required()}
    if not isinstance(required, list) or set(required) != expected_required or not all(isinstance(item, str) for item in required):
        raise _invalid_schema_response()
    _validate_scalar_schema(properties.get("name"), expected_kind="string")
    _validate_scalar_schema(properties.get("system_prompt"), expected_kind="string")
    for section, model in _SECTION_MODELS.items():
        _validate_section_schema(properties.get(section), model=model)


def _validate_section_schema(value: object, *, model: type[BaseModel]) -> None:
    if not isinstance(value, dict) or set(value) != _OBJECT_SCHEMA_KEYS or value.get("type") != "object":
        raise _invalid_schema_response()
    properties = value.get("properties")
    if not isinstance(properties, dict) or set(properties) != set(model.model_fields):
        raise _invalid_schema_response()
    model_properties = model.model_json_schema().get("properties")
    if not isinstance(model_properties, dict):
        raise _invalid_schema_response()
    for name, field_schema in properties.items():
        expected_schema = model_properties.get(name)
        if not isinstance(name, str) or not isinstance(expected_schema, dict):
            raise _invalid_schema_response()
        _validate_scalar_schema(field_schema, expected_kind=_scalar_kind(expected_schema))


def _validate_scalar_schema(value: object, *, expected_kind: str) -> None:
    if not isinstance(value, dict) or not set(value) <= _SCALAR_SCHEMA_KEYS:
        raise _invalid_schema_response()
    if _scalar_kind(value) != expected_kind:
        raise _invalid_schema_response()
    for key in ("title", "description"):
        if key in value and not isinstance(value[key], str):
            raise _invalid_schema_response()
    if "format" in value and value["format"] != "textarea":
        raise _invalid_schema_response()
    if "default" in value and isinstance(value["default"], (dict, list)):
        raise _invalid_schema_response()


def _scalar_kind(value: dict[object, object]) -> str:
    direct = value.get("type")
    if direct in _SCALAR_TYPES:
        return str(direct)
    alternatives = value.get("anyOf")
    if not isinstance(alternatives, list) or len(alternatives) != 2:
        raise _invalid_schema_response()
    if not all(isinstance(item, dict) and set(item) == {"type"} for item in alternatives):
        raise _invalid_schema_response()
    types = {item.get("type") for item in alternatives if isinstance(item, dict)}
    scalar = types - {"null"}
    if "null" not in types or len(scalar) != 1 or not scalar <= _SCALAR_TYPES:
        raise _invalid_schema_response()
    return str(next(iter(scalar)))


def _invalid_schema_response() -> RuntimeUpstreamError:
    return RuntimeUpstreamError(502, b'{"detail":"Runtime returned an unsupported Agent schema"}')
