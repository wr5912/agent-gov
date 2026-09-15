from __future__ import annotations

from collections.abc import Mapping

from app.openapi_request_examples import REQUEST_EXAMPLE_CONTRACTS
from jsonschema import Draft202012Validator
from scripts.export_openapi import build_openapi_schema

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})


def _references(fragment: object) -> set[str]:
    found: set[str] = set()
    if isinstance(fragment, Mapping):
        reference = fragment.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            found.add(reference.rsplit("/", 1)[-1])
        for child in fragment.values():
            found.update(_references(child))
    elif isinstance(fragment, list):
        for child in fragment:
            found.update(_references(child))
    return found


def _has_example(fragment: Mapping[str, object]) -> bool:
    return "example" in fragment or bool(fragment.get("examples"))


def test_every_public_request_body_and_parameter_has_semantic_documentation() -> None:
    schema = build_openapi_schema()
    components = schema["components"]["schemas"]
    body_operations: set[tuple[str, str]] = set()
    reachable_components: set[str] = set()

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            for parameter in operation.get("parameters", []):
                assert str(parameter.get("description", "")).strip(), (
                    f"{method.upper()} {path} parameter {parameter.get('in')}:{parameter.get('name')} missing description"
                )
                assert _has_example(parameter), f"{method.upper()} {path} parameter {parameter.get('in')}:{parameter.get('name')} missing example"

            request_body = operation.get("requestBody")
            if not isinstance(request_body, Mapping):
                continue
            body_operations.add((path, method))
            assert str(request_body.get("description", "")).strip(), f"{method.upper()} {path} requestBody missing description"
            for media_type, media in request_body["content"].items():
                assert media.get("examples"), f"{method.upper()} {path} {media_type} missing named examples"
                body_schema = media.get("schema", {})
                reachable_components.update(_references(body_schema))
                for field_name, field_schema in body_schema.get("properties", {}).items():
                    assert str(field_schema.get("description", "")).strip(), f"{method.upper()} {path} inline field {field_name} missing description"
                    assert _has_example(field_schema), f"{method.upper()} {path} inline field {field_name} missing example"

    assert set(REQUEST_EXAMPLE_CONTRACTS) == body_operations

    queue = list(reachable_components)
    while queue:
        component_name = queue.pop()
        component = components[component_name]
        nested = _references(component) - reachable_components
        reachable_components.update(nested)
        queue.extend(nested)

    for component_name in sorted(reachable_components):
        component = components[component_name]
        assert str(component.get("description", "")).strip(), f"request component {component_name} missing description"
        for field_name, field_schema in component.get("properties", {}).items():
            assert str(field_schema.get("description", "")).strip(), f"request component {component_name}.{field_name} missing description"
            assert _has_example(field_schema), f"request component {component_name}.{field_name} missing example"


def test_governance_and_agentscope_examples_cover_high_risk_journeys() -> None:
    schema = build_openapi_schema()

    expected_example_names = {
        ("/api/runtime/sessions/", "post"): {"create_published_agent_session"},
        ("/api/runtime/sessions/{session_id}", "patch"): {"rename_session"},
        ("/api/runtime/chat/", "post"): {"agent_scope_message", "resume_user_confirmation"},
        ("/api/feedback-cases", "post"): {"from_feedback_signal"},
        ("/api/improvements", "post"): {"from_feedback"},
        ("/api/agent-change-sets", "post"): {"current_published_base", "explicit_base"},
        ("/api/agent-change-sets/{change_set_id}/approve", "post"): {"approve_reviewed_change_set"},
        ("/api/agent-change-sets/{change_set_id}/publish", "post"): {"normal_publish", "force_publish"},
    }
    for (path, method), expected in expected_example_names.items():
        examples = schema["paths"][path][method]["requestBody"]["content"]["application/json"]["examples"]
        assert set(examples) == expected

    hitl = schema["paths"]["/api/runtime/chat/"]["post"]["requestBody"]["content"]["application/json"]["examples"]["resume_user_confirmation"]["value"]
    assert hitl["input"]["type"] == "USER_CONFIRM_RESULT"
    assert hitl["input"]["reply_id"] == "reply-id-from-require-user-confirm"
    assert "rules" not in hitl["input"]["confirm_results"][0]


def test_native_field_examples_match_native_types_without_redefining_schemas() -> None:
    schema = build_openapi_schema()
    components = schema["components"]["schemas"]
    for name in ("Base64Source", "URLSource", "Msg", "ToolCallBlock", "ToolResultBlock", "DataBlock", "ErrorInfo", "ConfirmResult"):
        for field, contract in components[name]["properties"].items():
            validator = Draft202012Validator({**contract, "components": schema["components"]})
            for example in contract.get("examples", []):
                errors = list(validator.iter_errors(example))
                assert errors == [], f"{name}.{field} has a documented example outside the native type"
    assert components["ToolCallBlock"]["properties"]["input"]["type"] == "string"
    assert components["Msg"]["required"] == ["name", "content", "role"]


def test_request_examples_do_not_register_removed_runtime_routes() -> None:
    removed_prefixes = (
        "/api/chat",
        "/api/agent-runtime/",
        "/api/sessions",
        "/api/claude-user-input-requests",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/conversations",
        "/v1/agentgov/confirmation-requests",
    )

    assert all(not path.startswith(removed_prefixes) for path, _method in REQUEST_EXAMPLE_CONTRACTS)
