"""AgentScope 原子切换后的最小 OpenAPI 后处理契约。"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from http import HTTPStatus

from fastapi import FastAPI

from app.openapi_input_documentation import apply_request_input_documentation
from app.openapi_request_examples import REQUEST_EXAMPLE_CONTRACTS
from app.sse_contracts import RUNTIME_STREAM_PATH, runtime_sse_contract

OpenApiObject = dict[str, object]
OpenApiMapping = Mapping[str, object]
OpenApiMutableMapping = MutableMapping[str, object]

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
HTTP_ERROR_COMPONENT = "HttpErrorResponse"
DOMAIN_ERROR_COMPONENT = "DomainErrorResponse"

RUNTIME_SESSIONS_PATH = "/api/runtime/sessions/"
RUNTIME_CHAT_PATH = "/api/runtime/chat/"
AGENT_RUN_PATH = "/api/agent-runs/{run_id}"
AGENT_RUN_BY_OPERATION_PATH = "/api/agent-runs/by-client-operation"
AGENT_RUN_TRACE_PATH = "/api/agent-runs/{run_id}/trace"

REMOVED_RUNTIME_PATHS = frozenset(
    {
        "/api/chat",
        "/api/chat/stream",
        "/api/agent-runtime/sdk-events",
        "/api/debug/agent-runtime/raw-events",
        "/api/sessions",
        "/api/claude-user-input-requests",
        "/api/settings/openai-compat-agent",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/conversations",
        "/v1/agentgov/confirmation-requests",
    }
)

_HTTP_ERROR_SCHEMA: OpenApiObject = {
    "title": HTTP_ERROR_COMPONENT,
    "type": "object",
    "required": ["detail", "error_code"],
    "properties": {
        "detail": {
            "title": "Detail",
            "description": "Human-readable error detail; validation errors may use FastAPI's structured list.",
        },
        "error_code": {
            "title": "Error Code",
            "type": "string",
            "description": "Stable AgentGov error code.",
        },
    },
}

_DOMAIN_ERROR_SCHEMA: OpenApiObject = {
    **_HTTP_ERROR_SCHEMA,
    "title": DOMAIN_ERROR_COMPONENT,
    "additionalProperties": True,
    "description": "AgentGov domain error; extra fields may carry non-sensitive diagnostics.",
}

_ERROR_DESCRIPTIONS = {
    400: "Business rule violation or malformed domain request.",
    401: "Invalid or missing Bearer API key.",
    403: "Authenticated client is not allowed to access the requested resource.",
    404: "Requested resource was not found.",
    409: "Request conflicts with the current resource state.",
    413: "Payload is too large.",
    415: "Unsupported media type or text encoding.",
    422: "Request validation or semantic validation failed.",
    500: "AgentGov data integrity error.",
    502: "AgentScope Runtime returned an invalid or failed response.",
    503: "AgentScope Runtime is temporarily unavailable.",
    504: "Operation did not reach a durable terminal state before timeout.",
}

_EXPLICIT_ERROR_STATUSES: dict[tuple[str, str], frozenset[int]] = {
    (RUNTIME_SESSIONS_PATH, "post"): frozenset({409, 422, 502, 503}),
    (RUNTIME_SESSIONS_PATH, "get"): frozenset({422, 502, 503}),
    ("/api/runtime/agents/{governance_agent_id}/current", "get"): frozenset({404, 409}),
    ("/api/runtime/agents/{governance_agent_id}/provision", "post"): frozenset({404, 409, 502, 503}),
    ("/api/runtime/sessions/{session_id}/messages", "get"): frozenset({404, 409, 502, 503}),
    ("/api/runtime/sessions/{session_id}/status", "get"): frozenset({404, 409, 502, 503}),
    (RUNTIME_STREAM_PATH, "get"): frozenset({404, 409, 502, 503}),
    (RUNTIME_CHAT_PATH, "post"): frozenset({404, 409, 422, 502, 503}),
    ("/api/runtime/sessions/{session_id}/interrupt", "post"): frozenset({404, 409, 502, 503}),
    ("/api/runtime/sessions/{session_id}", "delete"): frozenset({404, 409, 502, 503}),
    (AGENT_RUN_PATH, "get"): frozenset({404}),
    (AGENT_RUN_BY_OPERATION_PATH, "get"): frozenset({404, 409}),
    (AGENT_RUN_TRACE_PATH, "get"): frozenset({404}),
    ("/api/agent-runs/{run_id}/cancel", "post"): frozenset({404, 409, 502, 503}),
}

NON_200_SUCCESS_CODES: dict[tuple[str, str], str] = {
    (RUNTIME_SESSIONS_PATH, "post"): "201",
    ("/api/runtime/sessions/{session_id}/interrupt", "post"): "202",
    ("/api/runtime/sessions/{session_id}", "delete"): "204",
    ("/api/agent-runs/{run_id}/cancel", "post"): "202",
    ("/api/agent-test-runs", "post"): "202",
    ("/api/agent-change-sets/{change_set_id}/test-runs", "post"): "202",
    ("/api/agent-test-sessions", "post"): "201",
    ("/api/agent-test-sessions/{test_session_id}", "delete"): "204",
    ("/api/improvements", "post"): "201",
    ("/api/improvements/{improvement_id}", "delete"): "204",
    ("/api/improvements/{improvement_id}/split", "post"): "201",
    ("/api/improvements/{improvement_id}/feedbacks", "post"): "201",
    ("/api/improvements/{improvement_id}/attach-feedback-case", "post"): "201",
    ("/api/assets", "post"): "201",
    ("/api/assets/{asset_id}/inherit", "post"): "201",
}


def install_openapi_contract(app: FastAPI) -> None:
    """安装幂等的 OpenAPI 后处理器。"""

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
        if not isinstance(path, str) or not isinstance(path_item, MutableMapping):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, MutableMapping):
                continue
            _document_request_examples(path, method, operation)
            if path == RUNTIME_STREAM_PATH and method == "get":
                _document_native_runtime_stream(operation)
            elif path.startswith(("/api/runtime/", "/api/agent-runs/")):
                _document_runtime_json_success(path, method, operation)
            for status_code in sorted(expected_error_statuses(path, method, operation)):
                _add_error_response(operation, status_code)
    apply_request_input_documentation(schema)


def expected_error_statuses(path: str, method: str, operation: OpenApiMapping) -> set[int]:
    statuses: set[int] = set(_EXPLICIT_ERROR_STATUSES.get((path, method), ()))
    if operation.get("security"):
        statuses.add(401)
    if "422" in _mapping(operation.get("responses", {})):
        statuses.add(422)
    if path.startswith(
        (
            "/api/agent-registry",
            "/api/agent-test-",
            "/api/improvements",
            "/api/assets",
            "/api/feedback-",
            "/api/agent-jobs",
            "/api/agent-change-sets",
            "/api/agent-releases",
        )
    ):
        if "{" in path:
            statuses.add(404)
        if method in {"post", "put", "patch", "delete"}:
            statuses.update({400, 409})
    return statuses


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


def _document_request_examples(path: str, method: str, operation: OpenApiMutableMapping) -> None:
    contract = REQUEST_EXAMPLE_CONTRACTS.get((path, method))
    if contract is None:
        return
    request_body = _mapping(operation.get("requestBody", {}))
    content = _mapping(request_body.get("content", {}))
    media = _mapping(content.get(contract.media_type, {}))
    media["examples"] = deepcopy(dict(contract.examples))
    if contract.operation_description and not operation.get("description"):
        operation["description"] = contract.operation_description


def _document_native_runtime_stream(operation: OpenApiMutableMapping) -> None:
    responses = _mapping(operation.setdefault("responses", {}))
    success = _mapping(responses.setdefault("200", {"description": "Successful Response"}))
    success["description"] = "Byte-for-byte proxy of the AgentScope AgentEvent stream."
    success["content"] = {
        "text/event-stream": {
            "schema": {
                "type": "string",
                "description": "Native AgentScope SSE bytes; unknown events are preserved.",
            },
            "examples": {
                "native_event": {
                    "summary": "Native AgentScope event",
                    "value": runtime_sse_contract()["example"],
                }
            },
        }
    }
    operation["x-agentgov-sse-contract"] = runtime_sse_contract()


def _document_runtime_json_success(path: str, method: str, operation: OpenApiMutableMapping) -> None:
    if method == "delete":
        return
    responses = _mapping(operation.setdefault("responses", {}))
    status_code = NON_200_SUCCESS_CODES.get((path, method), "200")
    success = _mapping(responses.get(status_code, {}))
    content = _mapping(success.get("content", {}))
    media = _mapping(content.get("application/json", {}))
    schema = media.get("schema")
    if schema == {} or schema is None:
        media["schema"] = _runtime_success_schema(path, method)
        content["application/json"] = media
        success["content"] = content
        responses[status_code] = success
    header_names: tuple[str, ...] = ()
    if (path, method) in {
        (RUNTIME_SESSIONS_PATH, "post"),
        ("/api/runtime/sessions/{session_id}/interrupt", "post"),
    }:
        header_names = ("X-AgentGov-Session-Id",)
    elif (path, method) in {
        (RUNTIME_CHAT_PATH, "post"),
        ("/api/agent-runs/{run_id}/cancel", "post"),
    }:
        header_names = ("X-AgentGov-Run-Id", "X-AgentGov-Session-Id")
    if header_names:
        headers = _mapping(success.setdefault("headers", {}))
        for name in header_names:
            headers.setdefault(
                name,
                {
                    "description": "AgentGov-owned correlation identifier.",
                    "schema": {"type": "string"},
                },
            )


def _runtime_success_schema(path: str, method: str) -> OpenApiObject:
    if (path, method) == (RUNTIME_SESSIONS_PATH, "post"):
        return {
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "string"}},
            "additionalProperties": True,
        }
    if (path, method) == (RUNTIME_SESSIONS_PATH, "get"):
        return {
            "type": "object",
            "required": ["sessions", "total"],
            "properties": {
                "sessions": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "total": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        }
    if (path, method) == (AGENT_RUN_PATH, "get"):
        return {"$ref": "#/components/schemas/AgentRunResponse"}
    return {"type": "object", "additionalProperties": True}


def _add_error_response(operation: OpenApiMutableMapping, status_code: int) -> None:
    responses = _mapping(operation.setdefault("responses", {}))
    key = str(status_code)
    if status_code == 422 and key in responses:
        existing = _mapping(responses[key])
        existing["description"] = _ERROR_DESCRIPTIONS[422]
        return
    component = HTTP_ERROR_COMPONENT if status_code in {401, 403, 413, 415, 500} else DOMAIN_ERROR_COMPONENT
    responses.setdefault(
        key,
        {
            "description": _ERROR_DESCRIPTIONS.get(status_code, HTTPStatus(status_code).phrase),
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{component}"}}},
        },
    )


def _mapping(value: object) -> OpenApiMutableMapping:
    return value if isinstance(value, MutableMapping) else {}
