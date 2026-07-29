from __future__ import annotations

# ruff: noqa: E402
import argparse
import copy
import json
import sys
from pathlib import Path
from typing import get_args
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.openapi_contract import (
    CHAT_STREAM_PATH,
    CLAUDE_SDK_EVENTS_PATH,
    NON_200_SUCCESS_CODES,
    RESPONSES_PATH,
    expected_error_statuses,
    operation_items,
)
from app.openapi_request_examples import REQUEST_EXAMPLE_CONTRACTS
from app.runtime.agent_job_types import AgentJobType
from app.runtime.asset_schemas import ASSET_TYPES
from app.runtime.records.agent_job_records import HISTORICAL_AGENT_JOB_STATES
from app.runtime.records.claude_user_input_records import STATUSES as CLAUDE_USER_INPUT_STATUSES
from app.runtime.records.source_records import (
    FeedbackSignalSourceType,
    FeedbackSourceKind,
    SocEventType,
)
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentGitFileDiffStatus,
    AgentRepositoryHealthStatus,
)
from app.runtime.state_machines import (
    AGENT_CHANGE_SET_STATES,
    AGENT_RELEASE_STATES,
    AGENT_TEST_RUN_STATES,
    CASE_STATES,
    IMPROVEMENT_STAGES,
    PENDING_CORRELATION_STATES,
    AgentLifecycleTargetStatus,
)
from app.sse_contracts import sse_event_contract
from scripts.export_openapi import build_openapi_schema

OpenApiObject = dict[str, object]


def main() -> int:
    args = _parse_args()
    schema = _load_schema(args)
    expected_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    issues = audit_schema(schema, expected_version=expected_version)
    if args.base_url and args.compare_local:
        issues.extend(audit_live_matches_local(schema, build_openapi_schema()))

    if issues:
        for issue in issues:
            print(f"OPENAPI_CONTRACT_FAIL: {issue}")
        return 1 if args.fail else 0
    print(
        f"openapi contract OK: openapi={schema.get('openapi')} info.version={schema.get('info', {}).get('version')} operations={len(operation_items(schema))}"
    )
    return 0


def audit_schema(schema: OpenApiObject, *, expected_version: str | None = None) -> list[str]:
    issues: list[str] = []
    dialect = schema.get("openapi")
    if not isinstance(dialect, str) or not dialect.startswith("3.1."):
        issues.append(f"openapi dialect {dialect!r} is not the required 3.1.x contract")
    if expected_version:
        actual_version = _info_version(schema)
        if actual_version != expected_version:
            issues.append(f"info.version {actual_version!r} != VERSION {expected_version!r}")
    for path, method, operation in operation_items(schema):
        responses = _responses(operation)
        for status_code in sorted(expected_error_statuses(path, method, operation)):
            if str(status_code) not in responses:
                issues.append(f"{method.upper()} {path} missing documented {status_code} response")
        issues.extend(_audit_empty_success_schema(path, method, responses))
        if path.startswith(("/api/", "/v1/")) and not operation.get("security"):
            issues.append(f"{method.upper()} {path} missing Bearer security declaration")
    issues.extend(_audit_streaming_media_types(schema))
    issues.extend(_audit_request_examples(schema))
    issues.extend(_audit_closed_request_values(schema))
    issues.extend(_audit_request_components(schema))
    issues.extend(_audit_reviewed_operations(schema))
    issues.extend(_audit_sse_contracts(schema))
    issues.extend(_audit_non_200_success_codes(schema))
    return issues


def _audit_request_examples(schema: OpenApiObject) -> list[str]:
    issues: list[str] = []
    body_operations = {(path, method) for path, method, operation in operation_items(schema) if isinstance(operation.get("requestBody"), dict)}
    registered_operations = set(REQUEST_EXAMPLE_CONTRACTS)
    for path, method in sorted(body_operations - registered_operations):
        issues.append(f"{method.upper()} {path} request body has no registered named examples")
    for path, method in sorted(registered_operations - body_operations):
        issues.append(f"stale request-example registration for missing {method.upper()} {path}")

    for path, method in sorted(body_operations & registered_operations):
        operation = _operation(schema, path, method)
        if not isinstance(operation.get("description"), str) or not operation["description"].strip():
            issues.append(f"{method.upper()} {path} request body operation has no meaningful description")
        contract = REQUEST_EXAMPLE_CONTRACTS[(path, method)]
        request_body = operation.get("requestBody")
        content = request_body.get("content") if isinstance(request_body, dict) else None
        media = content.get(contract.media_type) if isinstance(content, dict) else None
        examples = media.get("examples") if isinstance(media, dict) else None
        expected_examples = dict(contract.examples)
        if examples != expected_examples:
            issues.append(f"{method.upper()} {path} named request examples differ from the centralized registry")
        if not isinstance(examples, dict):
            continue
        for example_name, example in examples.items():
            summary = example.get("summary") if isinstance(example, dict) else None
            if not isinstance(summary, str) or not summary.strip():
                issues.append(f"{method.upper()} {path} example {example_name!r} has no summary")
                continue
            value = example.get("value")
            for pointer, reason in _placeholder_issues(value):
                issues.append(f"{method.upper()} {path} example {example_name!r}{pointer}: {reason}")
    return issues


def _placeholder_issues(value: object, *, pointer: str = "") -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    if value is None:
        return [(pointer or "/", "null placeholder is forbidden; omit unknown optional fields")]
    if isinstance(value, str):
        if value.strip().lower() == "string":
            issues.append((pointer or "/", "generic string placeholder is forbidden"))
        return issues
    if isinstance(value, list):
        for index, item in enumerate(value):
            issues.extend(_placeholder_issues(item, pointer=f"{pointer}/{index}"))
        return issues
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{pointer}/{_json_pointer_token(key)}"
            if str(key).startswith("additionalProp") and str(key)[14:].isdigit():
                issues.append((child, "generated additionalProp placeholder is forbidden"))
            issues.extend(_placeholder_issues(item, pointer=child))
    return issues


def _audit_closed_request_values(schema: OpenApiObject) -> list[str]:
    issues: list[str] = []
    parameter_contracts = {
        ("/api/claude-user-input-requests", "get", "status"): CLAUDE_USER_INPUT_STATUSES,
        ("/api/agent-change-sets", "get", "status"): AGENT_CHANGE_SET_STATES,
        ("/api/agent-releases", "get", "status"): AGENT_RELEASE_STATES,
        ("/api/agent-test-runs/history", "get", "status"): AGENT_TEST_RUN_STATES,
        ("/api/assets", "get", "asset_type"): set(ASSET_TYPES),
        ("/api/agent-jobs", "get", "job_type"): {item.value for item in AgentJobType},
        ("/api/agent-jobs", "get", "status"): HISTORICAL_AGENT_JOB_STATES,
        ("/api/feedback-cases", "get", "status"): CASE_STATES,
        ("/api/feedback-signals", "get", "source_type"): set(get_args(FeedbackSignalSourceType)),
        ("/api/soc-events", "get", "event_type"): set(get_args(SocEventType)),
        ("/api/pending-correlations", "get", "status"): PENDING_CORRELATION_STATES,
        (
            "/api/feedback-sources/{source_kind}/{source_id}",
            "get",
            "source_kind",
        ): set(get_args(FeedbackSourceKind)),
        (
            "/api/feedback-sources/{source_kind}/{source_id}",
            "patch",
            "source_kind",
        ): set(get_args(FeedbackSourceKind)),
    }
    for (path, method, parameter_name), expected in parameter_contracts.items():
        operation = _operation(schema, path, method)
        parameter = next(
            (item for item in operation.get("parameters", []) if isinstance(item, dict) and item.get("name") == parameter_name),
            None,
        )
        actual = _enum_values(parameter.get("schema") if isinstance(parameter, dict) else None, schema)
        if actual != set(expected):
            issues.append(f"{method.upper()} {path} parameter {parameter_name} enum {sorted(actual)} != {sorted(expected)}")

    component_contracts = {
        ("AgentLifecycleTransitionRequest", "status"): set(get_args(AgentLifecycleTargetStatus)),
        ("ImprovementStageTransitionRequest", "stage"): IMPROVEMENT_STAGES,
        ("AssetCreateRequest", "asset_type"): set(ASSET_TYPES),
        ("AgentRepositoryStatusResponse", "status"): set(get_args(AgentRepositoryHealthStatus)),
        ("AgentGitFileDiffResponse", "status"): set(get_args(AgentGitFileDiffStatus)),
        ("AgentChangeSetResponse", "status"): AGENT_CHANGE_SET_STATES,
        ("AgentReleaseResponse", "status"): AGENT_RELEASE_STATES,
    }
    components = _component_schemas(schema)
    for (component_name, property_name), expected in component_contracts.items():
        component = components.get(component_name)
        actual = _enum_values(_property_schema(component, property_name), schema)
        if actual != set(expected):
            issues.append(f"component {component_name}.{property_name} enum {sorted(actual)} != {sorted(expected)}")
    return issues


def _enum_values(fragment: object, schema: OpenApiObject) -> set[str]:
    if not isinstance(fragment, dict):
        return set()
    values = fragment.get("enum")
    found = {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()
    ref = fragment.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        found.update(
            _enum_values(
                _component_schemas(schema).get(ref.rsplit("/", 1)[-1]),
                schema,
            )
        )
    for keyword in ("anyOf", "oneOf", "allOf"):
        children = fragment.get(keyword)
        if isinstance(children, list):
            for child in children:
                found.update(_enum_values(child, schema))
    return found


def audit_live_matches_local(live_schema: OpenApiObject, local_schema: OpenApiObject) -> list[str]:
    live = _canonical_schema(live_schema)
    local = _canonical_schema(local_schema)
    return [
        f"live/local semantic diff at {pointer}: local={expected!r} live={actual!r}" for pointer, expected, actual in _json_differences(local, live, limit=100)
    ]


def _audit_request_components(schema: OpenApiObject) -> list[str]:
    issues: list[str] = []
    components = _component_schemas(schema)
    for name in ("AgentTargetedChatRequest", "ChatStreamRequest", "ClaudeSdkEventsRequest"):
        component = components.get(name)
        required = set(component.get("required", [])) if isinstance(component, dict) else set()
        if not {"message", "agent_id"} <= required:
            issues.append(f"component {name} must require message and agent_id")
        issues.extend(_audit_non_blank_property(component, name=name, property_name="message"))
        issues.extend(_audit_non_blank_property(component, name=name, property_name="agent_id"))

    extension = components.get("AgentGovRequestExtension")
    extension_required = set(extension.get("required", [])) if isinstance(extension, dict) else set()
    if "agent_id" not in extension_required:
        issues.append("component AgentGovRequestExtension must require agent_id")
    issues.extend(_audit_non_blank_property(extension, name="AgentGovRequestExtension", property_name="agent_id"))

    completion = components.get("OpenAIChatCompletionRequest")
    stream = _property_schema(completion, "stream")
    if stream.get("const") is not False:
        issues.append("component OpenAIChatCompletionRequest.stream must declare const=false")

    conversation = components.get("ConversationCreateRequest")
    if not isinstance(conversation, dict) or conversation.get("additionalProperties") is not False:
        issues.append("component ConversationCreateRequest must reject unknown fields")

    responses_request = components.get("ResponsesRequest")
    if not isinstance(responses_request, dict) or not responses_request.get("oneOf") or not responses_request.get("allOf"):
        issues.append("component ResponsesRequest must document strict/control and speech-summary conditions")
    issues.extend(_audit_non_blank_property(responses_request, name="ResponsesRequest", property_name="input"))
    return issues


def _audit_non_blank_property(component: object, *, name: str, property_name: str) -> list[str]:
    prop = _property_schema(component, property_name)
    candidate_schemas = [prop]
    for key in ("anyOf", "oneOf"):
        values = prop.get(key)
        if isinstance(values, list):
            candidate_schemas.extend(value for value in values if isinstance(value, dict))
    constrained = any(
        isinstance(candidate.get("minLength"), int) and candidate["minLength"] >= 1 and isinstance(candidate.get("pattern"), str) and candidate["pattern"]
        for candidate in candidate_schemas
    )
    return [] if constrained else [f"component {name}.{property_name} must expose minLength and a non-blank pattern"]


def _audit_reviewed_operations(schema: OpenApiObject) -> list[str]:
    issues: list[str] = []
    paths = _paths(schema)
    removed_hitl_paths = {
        "/api/claude-hitl-requests",
        "/api/claude-hitl-requests/{request_id}/decision",
        "/api/claude-user-input-requests/{request_id}/decision",
    }
    for path in sorted(removed_hitl_paths & set(paths)):
        issues.append(f"removed HITL alias still exposed: {path}")

    reviewed = (
        ("/v1/conversations", "post"),
        ("/v1/conversations", "get"),
        ("/v1/conversations/{conversation_id}", "get"),
        ("/v1/conversations/{conversation_id}", "delete"),
        ("/v1/conversations/{conversation_id}/items", "get"),
        ("/api/sessions", "get"),
        ("/api/sessions/{session_id}/messages", "get"),
        ("/api/sessions/{session_id}", "delete"),
        ("/api/claude-user-input-requests", "get"),
        ("/v1/agentgov/confirmation-requests/{request_id}/decision", "post"),
    )
    for path, method in reviewed:
        operation = _operation(schema, path, method)
        if not isinstance(operation.get("description"), str) or not operation["description"].strip():
            issues.append(f"{method.upper()} {path} missing non-empty description")

    for path, method in (
        ("/api/sessions", "get"),
        ("/api/sessions/{session_id}/messages", "get"),
        ("/api/sessions/{session_id}", "delete"),
    ):
        if _operation(schema, path, method).get("deprecated") is not True:
            issues.append(f"{method.upper()} {path} must be deprecated")

    responses_operation = _operation(schema, RESPONSES_PATH, "post")
    if responses_operation.get("x-agentgov-contract-status") != "transitional":
        issues.append(f"POST {RESPONSES_PATH} must declare transitional contract status")
    deviations = responses_operation.get("x-agentgov-known-deviations")
    if not isinstance(deviations, list) or not deviations:
        issues.append(f"POST {RESPONSES_PATH} must document known transitional deviations")
    responses_wording = " ".join(str(responses_operation.get(field, "")) for field in ("summary", "description")).lower()
    if "canonical" in responses_wording:
        issues.append(f"POST {RESPONSES_PATH} must not claim canonical OpenAI Responses compatibility")
    if not {"transitional", "source of truth"} <= {phrase for phrase in ("transitional", "source of truth") if phrase in responses_wording}:
        issues.append(f"POST {RESPONSES_PATH} wording must state its transitional status and source-of-truth boundary")
    for tag in schema.get("tags", []):
        if isinstance(tag, dict) and tag.get("name") == "openai-responses" and "canonical" in str(tag.get("description", "")).lower():
            issues.append("openai-responses tag must not claim a canonical surface")
    return issues


def _audit_sse_contracts(schema: OpenApiObject) -> list[str]:
    issues: list[str] = []
    for path in (CHAT_STREAM_PATH, CLAUDE_SDK_EVENTS_PATH, RESPONSES_PATH):
        operation = _operation(schema, path, "post")
        actual = operation.get("x-agentgov-sse-events")
        expected = sse_event_contract(path)
        if actual != expected:
            issues.append(f"POST {path} x-agentgov-sse-events differs from the typed per-surface registry")
    return issues


def _audit_non_200_success_codes(schema: OpenApiObject) -> list[str]:
    issues: list[str] = []
    for (path, method), expected_status in NON_200_SUCCESS_CODES.items():
        responses = _responses(_operation(schema, path, method))
        success_statuses = {status for status in responses if status.startswith("2")}
        if success_statuses != {expected_status}:
            issues.append(f"{method.upper()} {path} success statuses {sorted(success_statuses)} != [{expected_status!r}]")
    return issues


def _audit_empty_success_schema(path: str, method: str, responses: OpenApiObject) -> list[str]:
    issues: list[str] = []
    for status_code, response in responses.items():
        if not status_code.startswith("2") or not isinstance(response, dict):
            continue
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        json_media = content.get("application/json")
        if isinstance(json_media, dict) and json_media.get("schema") == {}:
            issues.append(f"{method.upper()} {path} {status_code} documents empty application/json schema")
    return issues


def _audit_streaming_media_types(schema: OpenApiObject) -> list[str]:
    issues: list[str] = []
    chat_stream = _success_content(schema, CHAT_STREAM_PATH, "post")
    if "text/event-stream" not in chat_stream:
        issues.append(f"POST {CHAT_STREAM_PATH} missing text/event-stream 200 response")
    if "application/json" in chat_stream:
        issues.append(f"POST {CHAT_STREAM_PATH} still documents application/json 200 response")

    responses = _success_content(schema, RESPONSES_PATH, "post")
    if "application/json" not in responses:
        issues.append(f"POST {RESPONSES_PATH} missing application/json 200 response")
    if "text/event-stream" not in responses:
        issues.append(f"POST {RESPONSES_PATH} missing text/event-stream 200 response")
    return issues


def _success_content(schema: OpenApiObject, path: str, method: str) -> OpenApiObject:
    operation = _operation(schema, path, method)
    responses = _responses(operation if isinstance(operation, dict) else {})
    success = responses.get("200", {})
    if not isinstance(success, dict):
        return {}
    content = success.get("content")
    return content if isinstance(content, dict) else {}


def _responses(operation: object) -> OpenApiObject:
    if not isinstance(operation, dict):
        return {}
    responses = operation.get("responses")
    return responses if isinstance(responses, dict) else {}


def _info_version(schema: OpenApiObject) -> str | None:
    info = schema.get("info", {})
    return info.get("version") if isinstance(info, dict) and isinstance(info.get("version"), str) else None


def _component_schemas(schema: OpenApiObject) -> OpenApiObject:
    components = schema.get("components")
    if not isinstance(components, dict):
        return {}
    schemas = components.get("schemas")
    return schemas if isinstance(schemas, dict) else {}


def _property_schema(component: object, property_name: str) -> OpenApiObject:
    if not isinstance(component, dict):
        return {}
    properties = component.get("properties")
    if not isinstance(properties, dict):
        return {}
    prop = properties.get(property_name)
    return prop if isinstance(prop, dict) else {}


def _paths(schema: OpenApiObject) -> OpenApiObject:
    paths = schema.get("paths")
    return paths if isinstance(paths, dict) else {}


def _operation(schema: OpenApiObject, path: str, method: str) -> OpenApiObject:
    path_item = _paths(schema).get(path)
    if not isinstance(path_item, dict):
        return {}
    operation = path_item.get(method)
    return operation if isinstance(operation, dict) else {}


def _canonical_schema(schema: OpenApiObject) -> OpenApiObject:
    canonical = copy.deepcopy(schema)
    canonical.pop("servers", None)
    for key in tuple(canonical):
        if key.startswith("x-deployment-"):
            canonical.pop(key, None)
    return canonical


def _json_differences(
    expected: object,
    actual: object,
    *,
    pointer: str = "",
    limit: int,
) -> list[tuple[str, object, object]]:
    differences: list[tuple[str, object, object]] = []

    def walk(left: object, right: object, current: str) -> None:
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            differences.append((current or "/", left, right))
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                child = f"{current}/{_json_pointer_token(key)}"
                if key not in left:
                    differences.append((child, "<absent>", right[key]))
                elif key not in right:
                    differences.append((child, left[key], "<absent>"))
                else:
                    walk(left[key], right[key], child)
                if len(differences) >= limit:
                    return
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                differences.append((f"{current}/length", len(left), len(right)))
            for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
                walk(left_item, right_item, f"{current}/{index}")
                if len(differences) >= limit:
                    return
            return
        if left != right:
            differences.append((current or "/", left, right))

    walk(expected, actual, pointer)
    return differences


def _json_pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _load_schema(args: argparse.Namespace) -> OpenApiObject:
    if args.input:
        return _load_json(args.input)
    if args.base_url:
        url = args.base_url.rstrip("/") + "/openapi.json"
        with urlopen(url, timeout=args.timeout) as response:
            value = json.load(response)
        if not isinstance(value, dict):
            raise ValueError(f"{url} did not return a JSON object")
        return value
    return dict(build_openapi_schema())


def _load_json(path: Path) -> OpenApiObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit AgentGov OpenAPI schema against runtime contract rules.")
    parser.add_argument("--input", type=Path, help="Read an exported OpenAPI JSON file instead of building locally.")
    parser.add_argument("--base-url", help="Fetch /openapi.json from a running API base URL.")
    parser.add_argument(
        "--compare-local",
        action="store_true",
        help="When using --base-url, deep-compare the normalized live OpenAPI document with the local export.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--fail", action="store_true", help="Exit non-zero when contract issues are found.")
    args = parser.parse_args()
    if args.input and args.base_url:
        parser.error("--input and --base-url are mutually exclusive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
