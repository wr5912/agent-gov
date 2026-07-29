from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from http import HTTPStatus

from fastapi import FastAPI

from app.openapi_request_examples import REQUEST_EXAMPLE_CONTRACTS
from app.runtime.prepared_managed_stream import MANAGED_RUN_RESPONSE_HEADER_DESCRIPTIONS
from app.runtime.runtime_raw_events import RAW_EVENT_RESPONSE_HEADER_DESCRIPTIONS
from app.runtime.speech_summary import AgentGovSpeechSummaryEnvelope
from app.sse_contracts import (
    CHAT_STREAM_PATH,
    CLAUDE_SDK_EVENTS_PATH,
    RESPONSES_PATH,
    SSE_COMMENTS,
    SSE_EXAMPLES,
    sse_event_contract,
)

OpenApiObject = dict[str, object]
OpenApiMapping = Mapping[str, object]
OpenApiMutableMapping = MutableMapping[str, object]

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
HTTP_ERROR_COMPONENT = "HttpErrorResponse"
DOMAIN_ERROR_COMPONENT = "DomainErrorResponse"
OPENAI_ERROR_COMPONENT = "OpenAIErrorResponse"
VALIDATION_ERROR_COMPONENT = "HTTPValidationError"
SECURITY_SCHEME_NAME = "HTTPBearer"
RAW_EVENTS_PATH = "/api/debug/agent-runtime/raw-events"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

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

_OPENAI_ERROR_SCHEMA: OpenApiObject = {
    "title": OPENAI_ERROR_COMPONENT,
    "type": "object",
    "required": ["error"],
    "additionalProperties": False,
    "properties": {
        "error": {
            "type": "object",
            "required": ["message", "type", "code"],
            "additionalProperties": False,
            "properties": {
                "message": {"type": "string"},
                "type": {"type": "string"},
                "code": {"type": "string"},
            },
        }
    },
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
    501: "The host platform cannot provide byte-exact native Runtime capture.",
    502: "The selected Agent runtime failed to produce a compatible response.",
    503: "Configured runtime or model/agent target is temporarily unavailable.",
    504: "The requested operation did not reach its durable terminal state before the timeout.",
}

_MUTATING_METHODS = frozenset({"post", "put", "patch", "delete"})
_DOMAIN_PREFIXES = (
    "/api/agent-registry",
    "/api/agent-test-assets",
    "/api/agent-test-runs",
    "/api/agent-test-sessions",
    "/api/improvements",
    "/api/assets",
    "/api/langfuse/traces",
    "/api/agent-jobs",
    "/api/eval-",
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
    "/api/agent-runtime/sdk-events",
    "/v1/chat/completions",
    "/v1/responses",
    "/api/agent-repository",
    "/api/agent-change-sets",
    "/api/agent-releases",
)
_RUNTIME_OR_RELEASE_PATH_PARTS = ("/generate", "/execution/apply", "/test-runs", "/publish", "/restore", "/rollback")

_EXPLICIT_ERROR_STATUSES: dict[tuple[str, str], frozenset[int]] = {
    ("/api/chat", "post"): frozenset({400, 404, 409, 422, 503}),
    (CHAT_STREAM_PATH, "post"): frozenset({400, 404, 422, 503}),
    (CLAUDE_SDK_EVENTS_PATH, "post"): frozenset({400, 404, 422, 503}),
    (RAW_EVENTS_PATH, "post"): frozenset({400, 403, 404, 409, 413, 422, 501, 503}),
    (CHAT_COMPLETIONS_PATH, "post"): frozenset({400, 404, 422, 502, 503}),
    (RESPONSES_PATH, "post"): frozenset({400, 404, 409, 422, 503}),
    ("/api/agent-runs/{run_id}/cancel", "post"): frozenset({404, 409, 504}),
    ("/v1/responses/{response_id}", "get"): frozenset({404}),
    ("/v1/conversations/{conversation_id}", "get"): frozenset({404}),
    ("/v1/conversations/{conversation_id}", "delete"): frozenset({409}),
    ("/v1/conversations/{conversation_id}/items", "get"): frozenset({404, 409}),
    ("/api/sessions/{session_id}/messages", "get"): frozenset({404, 409}),
    ("/api/sessions/{session_id}", "delete"): frozenset({409}),
    ("/v1/agentgov/confirmation-requests/{request_id}/decision", "post"): frozenset({404, 409, 422}),
}

NON_200_SUCCESS_CODES: dict[tuple[str, str], str] = {
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
    schemas.setdefault(OPENAI_ERROR_COMPONENT, _OPENAI_ERROR_SCHEMA)
    _install_model_schema(schemas, AgentGovSpeechSummaryEnvelope)

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
    explicit = _EXPLICIT_ERROR_STATUSES.get((path, method))
    if explicit is not None:
        statuses.update(explicit)
    else:
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
    if path == "/api/agent-test-assets":
        return {409}
    if path == "/api/agent-registry/{agent_id}/test-suite/file":
        return {409, 413}
    if path == "/api/agent-test-runs/history":
        return {404}
    if method == "post" and path == "/api/agent-test-runs":
        return {404}
    if method == "post" and path == "/api/feedback-cases":
        return {404}
    if path in {"/api/config", "/api/agents", "/api/skills"}:
        return {404, 422}
    if path == "/api/agent-config-file":
        return {403, 404, 409, 413, 415, 422}
    if path == "/api/agent-registry/{agent_id}/workspace/import":
        return {411, 413, 415, 503}
    if path == "/api/agent-registry/{agent_id}/workspace/export":
        return {413}
    if path == "/api/agent-registry/{agent_id}/workspace/restore":
        return {413, 503}
    if path == "/api/settings/openai-compat-agent" and method == "put":
        return {400, 404, 422}
    return set()


def _can_return_runtime_unavailable(path: str) -> bool:
    if path in {
        "/api/chat",
        CHAT_STREAM_PATH,
        CLAUDE_SDK_EVENTS_PATH,
        RAW_EVENTS_PATH,
        "/v1/chat/completions",
        RESPONSES_PATH,
    }:
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
    _document_request_examples(path, method, operation)
    _fix_streaming_success_response(path, operation)
    _document_sse_events(path, operation)
    for status_code in sorted(expected_error_statuses(path, method, operation)):
        _add_error_response(path, operation, status_code)


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


def _fix_streaming_success_response(path: str, operation: OpenApiMutableMapping) -> None:
    if path not in {
        CHAT_STREAM_PATH,
        CLAUDE_SDK_EVENTS_PATH,
        RAW_EVENTS_PATH,
        RESPONSES_PATH,
    }:
        return
    responses = _mapping(operation.setdefault("responses", {}))
    success = _mapping(responses.setdefault("200", {"description": "Successful Response"}))
    if path in {CHAT_STREAM_PATH, CLAUDE_SDK_EVENTS_PATH}:
        success["description"] = "Server-sent event stream."
        description = "Claude Agent SDK-native SSE events" if path == CLAUDE_SDK_EVENTS_PATH else "Claude Agent Chat SSE events"
        success["content"] = {"text/event-stream": _sse_media_type(path, description)}
        _document_managed_run_headers(success)
        return
    if path == RAW_EVENTS_PATH:
        success["description"] = "Byte-exact native Runtime stdout. stream=false buffers the body; stream=true flushes the same byte sequence incrementally."
        success["content"] = {
            "application/octet-stream": {
                "schema": {
                    "type": "string",
                    "format": "binary",
                    "description": "Unparsed native Runtime stdout bytes.",
                }
            }
        }
        success["headers"] = {
            name: {
                "description": description,
                "schema": {"type": "string"},
            }
            for name, description in RAW_EVENT_RESPONSE_HEADER_DESCRIPTIONS.items()
        }
        return
    if path == RESPONSES_PATH:
        success["description"] = "JSON response when stream=false; server-sent events when stream=true."
        content = _mapping(success.setdefault("content", {}))
        content.setdefault("text/event-stream", _sse_media_type(path, "Transitional OpenAI Responses-shaped SSE events"))
        _document_managed_run_headers(success)


def _document_managed_run_headers(success: OpenApiMutableMapping) -> None:
    headers = _mapping(success.setdefault("headers", {}))
    for name, description in MANAGED_RUN_RESPONSE_HEADER_DESCRIPTIONS.items():
        headers[name] = {
            "description": description,
            "schema": {"type": "string"},
        }


def _add_error_response(path: str, operation: OpenApiMutableMapping, status_code: int) -> None:
    responses = _mapping(operation.setdefault("responses", {}))
    key = str(status_code)
    if status_code == 422 and key in responses:
        _extend_422_response(_mapping(responses[key]))
        return
    if path == CHAT_COMPLETIONS_PATH and status_code == 502:
        component = OPENAI_ERROR_COMPONENT
    else:
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


def _sse_media_type(path: str, description: str) -> OpenApiObject:
    return {
        "schema": {"type": "string", "description": description},
        "examples": {
            "event": {
                "summary": "Surface-specific SSE event",
                "value": SSE_EXAMPLES[path],
            }
        },
    }


def _document_sse_events(path: str, operation: OpenApiMutableMapping) -> None:
    events = sse_event_contract(path)
    if not events:
        return
    operation["x-agentgov-sse-events"] = events
    comments = SSE_COMMENTS.get(path)
    if comments:
        operation["x-agentgov-sse-comments"] = list(comments)
    if path == RESPONSES_PATH:
        operation["x-agentgov-contract-status"] = "transitional"
        operation["x-agentgov-known-deviations"] = [
            "This in-process adapter is Responses-shaped, not full OpenAI Responses compatibility.",
            "stream, non-stream, and retrieve do not guarantee identical metadata or trace projection.",
            "A source failure before session identity may produce response.created with id=null.",
            "Pre-session source failures are reported in-stream after HTTP 200 and cannot expose managed run headers.",
        ]


def _install_model_schema(schemas: OpenApiMutableMapping, model: type) -> None:
    generated = model.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = generated.pop("$defs", {})
    if isinstance(definitions, Mapping):
        for name, definition in definitions.items():
            schemas.setdefault(name, definition)
    schemas.setdefault(model.__name__, generated)


def _mapping(value: object) -> OpenApiMutableMapping:
    if isinstance(value, MutableMapping):
        return value
    return {}
