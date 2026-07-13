from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from http import HTTPStatus

from fastapi import FastAPI

OpenApiObject = dict[str, object]
OpenApiMapping = Mapping[str, object]
OpenApiMutableMapping = MutableMapping[str, object]

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
HTTP_ERROR_COMPONENT = "HttpErrorResponse"
DOMAIN_ERROR_COMPONENT = "DomainErrorResponse"
VALIDATION_ERROR_COMPONENT = "HTTPValidationError"
SECURITY_SCHEME_NAME = "HTTPBearer"
CHAT_STREAM_PATH = "/api/chat/stream"
RESPONSES_PATH = "/v1/responses"

_HTTP_ERROR_SCHEMA: OpenApiObject = {
    "title": HTTP_ERROR_COMPONENT,
    "type": "object",
    "required": ["detail", "error_code"],
    "properties": {
        "detail": {
            "title": "Detail",
            "description": "Human-readable error detail. FastAPI validation errors keep their native detail list.",
        },
        "error_code": {
            "title": "Error Code",
            "type": "string",
            "description": "Stable application error code returned by the app error handler.",
        },
    },
}

_DOMAIN_ERROR_SCHEMA: OpenApiObject = {
    **_HTTP_ERROR_SCHEMA,
    "title": DOMAIN_ERROR_COMPONENT,
    "additionalProperties": True,
    "description": "AgentGov domain error envelope. Extra top-level fields carry route-specific diagnostics.",
}

_ERROR_DESCRIPTIONS = {
    400: "Business rule violation or malformed domain request.",
    401: "Invalid or missing Bearer API key.",
    403: "Authenticated client is not allowed to access the requested resource.",
    404: "Requested AgentGov resource was not found.",
    409: "Request conflicts with the current resource state.",
    413: "Requested editable payload is too large.",
    415: "Requested editable payload uses an unsupported media or text encoding.",
    422: "Request validation error or route-level semantic validation error.",
    500: "AgentGov data integrity error returned through the HTTP error envelope.",
    503: "Configured runtime or model/agent target is temporarily unavailable.",
}

_MUTATING_METHODS = frozenset({"post", "put", "patch", "delete"})
_DOMAIN_PREFIXES = (
    "/api/agent-registry",
    "/api/improvements",
    "/api/assets",
    "/api/scenario-packs",
    "/api/langfuse/traces",
    "/api/agent-jobs",
    "/api/eval-",
    "/api/regression-assets",
    "/api/feedback-",
    "/api/evidence-packages",
    "/api/agent-runs",
    "/api/soc-events",
    "/api/pending-correlations",
    "/api/feedback-sources",
    "/api/asset-registry",
    "/api/agent-repository",
    "/api/agent-change-sets",
    "/api/agent-releases",
)
_RUNTIME_OR_RELEASE_PREFIXES = (
    "/api/chat",
    "/v1/chat/completions",
    "/v1/responses",
    "/api/agent-repository",
    "/api/agent-change-sets",
    "/api/agent-releases",
)
_RUNTIME_OR_RELEASE_PATH_PARTS = ("/generate", "/execution/apply", "/regression-runs", "/publish", "/restore", "/rollback")


def install_openapi_contract(app: FastAPI) -> None:
    """Install the AgentGov OpenAPI contract post-processor."""

    generate_openapi = app.openapi

    def custom_openapi() -> OpenApiObject:
        if app.openapi_schema:
            return app.openapi_schema
        schema = generate_openapi()
        apply_openapi_contract(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def apply_openapi_contract(schema: OpenApiMutableMapping) -> None:
    components = _mapping(schema.setdefault("components", {}))
    schemas = _mapping(components.setdefault("schemas", {}))
    schemas.setdefault(HTTP_ERROR_COMPONENT, _HTTP_ERROR_SCHEMA)
    schemas.setdefault(DOMAIN_ERROR_COMPONENT, _DOMAIN_ERROR_SCHEMA)

    paths = _mapping(schema.get("paths", {}))
    for path, path_item in paths.items():
        if not isinstance(path_item, MutableMapping):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, MutableMapping):
                continue
            _apply_operation_contract(path, method, operation)


def expected_error_statuses(path: str, method: str, operation: OpenApiMapping) -> set[int]:
    statuses: set[int] = set()
    if operation.get("security"):
        statuses.add(401)
    if "422" in _mapping(operation.get("responses", {})):
        statuses.add(422)
    statuses.update(_special_error_statuses(path, method))
    if any(path.startswith(prefix) for prefix in _DOMAIN_PREFIXES):
        if "{" in path:
            statuses.add(404)
        if method in _MUTATING_METHODS:
            statuses.update({400, 409})
    if _can_return_runtime_unavailable(path):
        statuses.add(503)
    return statuses


def _special_error_statuses(path: str, method: str) -> set[int]:
    if path == CHAT_STREAM_PATH or path == "/api/chat":
        return {400, 404, 422, 503}
    if path == "/v1/chat/completions":
        return {400, 404, 422, 503}
    if path == RESPONSES_PATH:
        return {400, 404, 409, 422, 503}
    if path == "/v1/responses/{response_id}":
        return {404}
    if path == "/v1/conversations/{conversation_id}/items":
        return {404, 409}
    if path == "/v1/conversations/{conversation_id}" and method != "delete":
        return {404}
    if path in {
        "/api/claude-user-input-requests/{request_id}/decision",
        "/v1/agentgov/confirmation-requests/{request_id}/decision",
    }:
        return {404, 409, 422}
    if path in {"/api/config", "/api/agents", "/api/skills"}:
        return {404, 422}
    if path == "/api/agent-config-file":
        return {403, 404, 409, 413, 415, 422}
    if path == "/api/settings/openai-compat-agent" and method == "put":
        return {400, 404, 422}
    if path == "/api/sessions/{session_id}/messages":
        return {404, 409}
    return set()


def _can_return_runtime_unavailable(path: str) -> bool:
    if path in {"/api/chat", CHAT_STREAM_PATH, "/v1/chat/completions", RESPONSES_PATH}:
        return True
    if any(path.startswith(prefix) for prefix in _RUNTIME_OR_RELEASE_PREFIXES):
        return any(part in path for part in _RUNTIME_OR_RELEASE_PATH_PARTS)
    if path.startswith("/api/improvements/"):
        return any(part in path for part in _RUNTIME_OR_RELEASE_PATH_PARTS)
    return False


def operation_items(schema: OpenApiMapping) -> list[tuple[str, str, OpenApiMapping]]:
    items: list[tuple[str, str, OpenApiMapping]] = []
    paths = schema.get("paths", {})
    if not isinstance(paths, Mapping):
        return items
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if isinstance(method, str) and method in HTTP_METHODS and isinstance(operation, Mapping):
                items.append((path, method, operation))
    return items


def _apply_operation_contract(path: str, method: str, operation: OpenApiMutableMapping) -> None:
    _fix_streaming_success_response(path, operation)
    for status_code in sorted(expected_error_statuses(path, method, operation)):
        _add_error_response(operation, status_code)


def _fix_streaming_success_response(path: str, operation: OpenApiMutableMapping) -> None:
    responses = _mapping(operation.setdefault("responses", {}))
    success = _mapping(responses.setdefault("200", {"description": "Successful Response"}))
    if path == CHAT_STREAM_PATH:
        success["description"] = "Server-sent event stream."
        success["content"] = {"text/event-stream": _sse_media_type("Claude Agent SSE events")}
        return
    if path == RESPONSES_PATH:
        success["description"] = "JSON response when stream=false; server-sent events when stream=true."
        content = _mapping(success.setdefault("content", {}))
        content.setdefault("text/event-stream", _sse_media_type("OpenAI Responses-style SSE events"))


def _add_error_response(operation: OpenApiMutableMapping, status_code: int) -> None:
    responses = _mapping(operation.setdefault("responses", {}))
    key = str(status_code)
    if status_code == 422 and key in responses:
        _extend_422_response(_mapping(responses[key]))
        return
    component = HTTP_ERROR_COMPONENT if status_code in {401, 403, 413, 415, 500} else DOMAIN_ERROR_COMPONENT
    responses.setdefault(
        key,
        {
            "description": _ERROR_DESCRIPTIONS.get(status_code, HTTPStatus(status_code).phrase),
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{component}"}}},
        },
    )


def _extend_422_response(response: OpenApiMutableMapping) -> None:
    response["description"] = _ERROR_DESCRIPTIONS[422]
    content = _mapping(response.setdefault("content", {}))
    json_content = _mapping(content.setdefault("application/json", {}))
    schema = json_content.get("schema")
    refs = [
        {"$ref": f"#/components/schemas/{VALIDATION_ERROR_COMPONENT}"},
        {"$ref": f"#/components/schemas/{HTTP_ERROR_COMPONENT}"},
    ]
    if isinstance(schema, Mapping) and schema.get("anyOf") == refs:
        return
    json_content["schema"] = {"anyOf": refs}


def _sse_media_type(description: str) -> OpenApiObject:
    return {
        "schema": {"type": "string", "description": description},
        "examples": {
            "event": {
                "summary": "SSE event",
                "value": 'event: message\ndata: {"type":"message"}\n\n',
            }
        },
    }


def _mapping(value: object) -> OpenApiMutableMapping:
    if isinstance(value, MutableMapping):
        return value
    return {}
