import copy

import pytest
from scripts.export_openapi import build_openapi_schema
from scripts.openapi_request_input_audit import audit_request_input_documentation


def test_all_live_request_inputs_have_descriptions_and_examples() -> None:
    schema = dict(build_openapi_schema())

    assert audit_request_input_documentation(schema) == []

    body_operations = 0
    named_examples = 0
    parameters = 0
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            request_body = operation.get("requestBody")
            if isinstance(request_body, dict):
                body_operations += 1
                for media in request_body["content"].values():
                    named_examples += len(media.get("examples", {}))
            parameters += len(operation.get("parameters", []))

    assert body_operations == 45
    assert named_examples == 63
    assert parameters == 166


def test_responses_swagger_guide_covers_every_nested_request_field() -> None:
    schema = build_openapi_schema()
    operation = schema["paths"]["/v1/responses"]["post"]
    description = operation["description"]
    speech = schema["components"]["schemas"]["AgentGovRequestExtension"]["properties"]["with_speech_summary"]

    assert "Parameters → No parameters" in description
    assert "Request body" in description
    assert description.count("\n| `") == 22
    assert "`agentgov.with_speech_summary`" in description
    assert "`agentgov.debug.sdk_raw`" in description
    assert "`input[].content[].text`" in description
    assert "top-level stream=true" in speech["description"]
    assert "422" in speech["description"]
    assert "best-effort" in speech["description"]
    assert speech["examples"] == [True]


def test_request_documentation_audit_reaches_arrays_unions_refs_and_multipart() -> None:
    schema = copy.deepcopy(build_openapi_schema())
    schema["components"]["schemas"]["ResponsesInputText"]["properties"]["text"].pop("examples")
    schema["paths"]["/api/agent-registry/{agent_id}/workspace/import"]["post"]["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ]["properties"]["package"].pop("description")

    issues = audit_request_input_documentation(schema)

    assert any("ResponsesInputText.text missing example" in issue for issue in issues)
    assert any("multipart/form-data.package missing description" in issue for issue in issues)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda schema: schema["paths"]["/v1/responses"]["post"]["requestBody"].pop("description"),
            "POST /v1/responses requestBody missing description",
        ),
        (
            lambda schema: schema["components"]["schemas"]["AgentGovDebug"].pop("description"),
            "request component AgentGovDebug missing description",
        ),
        (
            lambda schema: schema["components"]["schemas"]["AgentGovRequestExtension"]["properties"]["debug"].pop("description"),
            "AgentGovRequestExtension.debug missing description",
        ),
        (
            lambda schema: schema["paths"]["/api/agent-runs/{run_id}/cancel"]["post"]["parameters"][0].pop("example"),
            "parameter path:run_id missing example",
        ),
        (
            lambda schema: schema["paths"]["/api/agent-change-sets"]["get"]["parameters"][0].update(example="running"),
            "violates schema",
        ),
        (
            lambda schema: schema["components"]["schemas"]["ResponsesRequest"]["properties"]["model"].update(examples=["string"]),
            "generic string placeholder",
        ),
        (
            lambda schema: schema["paths"]["/v1/responses"]["post"].update(description="Request-body field guide removed."),
            "missing Swagger no-parameters explanation",
        ),
    ],
)
def test_request_documentation_audit_rejects_regressions(mutation, expected) -> None:
    schema = copy.deepcopy(build_openapi_schema())
    mutation(schema)

    assert any(expected in issue for issue in audit_request_input_documentation(schema))
