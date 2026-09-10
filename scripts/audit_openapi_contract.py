from __future__ import annotations

# ruff: noqa: E402
import argparse
import copy
import json
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.openapi_contract import (
    AGENT_RUN_PATH,
    AGENT_RUN_TRACE_PATH,
    NON_200_SUCCESS_CODES,
    REMOVED_RUNTIME_PATHS,
    RUNTIME_CHAT_PATH,
    RUNTIME_SESSIONS_PATH,
    expected_error_statuses,
    operation_items,
)
from app.sse_contracts import RUNTIME_STREAM_PATH, runtime_sse_contract

from scripts.export_openapi import build_openapi_schema

OpenApiObject = dict[str, object]

REQUIRED_RUNTIME_OPERATIONS = frozenset(
    {
        ("/api/runtime/agents/{governance_agent_id}/current", "get"),
        ("/api/runtime/agents/{governance_agent_id}/provision", "post"),
        (RUNTIME_SESSIONS_PATH, "post"),
        (RUNTIME_SESSIONS_PATH, "get"),
        ("/api/runtime/sessions/{session_id}/messages", "get"),
        ("/api/runtime/sessions/{session_id}/status", "get"),
        (RUNTIME_STREAM_PATH, "get"),
        (RUNTIME_CHAT_PATH, "post"),
        ("/api/runtime/sessions/{session_id}/interrupt", "post"),
        ("/api/runtime/sessions/{session_id}", "delete"),
        ("/api/agent-runs/by-client-operation", "get"),
        (AGENT_RUN_PATH, "get"),
        (AGENT_RUN_TRACE_PATH, "get"),
        ("/api/agent-runs/{run_id}/pending-actions", "get"),
        ("/api/agent-runs/{run_id}/cancel", "post"),
    }
)


def main() -> int:
    args = _parse_args()
    schema = _load_schema(args)
    expected_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    issues = audit_schema(schema, expected_version=expected_version)
    if args.base_url and args.compare_local:
        issues.extend(audit_live_matches_local(schema, dict(build_openapi_schema())))
    if issues:
        for issue in issues:
            print(f"OPENAPI_CONTRACT_FAIL: {issue}")
        return 1 if args.fail else 0
    print(f"openapi contract OK: openapi={schema.get('openapi')} info.version={_info_version(schema)} operations={len(operation_items(schema))}")
    return 0


def audit_schema(schema: OpenApiObject, *, expected_version: str | None = None) -> list[str]:
    issues: list[str] = []
    dialect = schema.get("openapi")
    if not isinstance(dialect, str) or not dialect.startswith("3.1."):
        issues.append(f"openapi dialect {dialect!r} is not the required 3.1.x contract")
    if expected_version and _info_version(schema) != expected_version:
        issues.append(f"info.version {_info_version(schema)!r} != VERSION {expected_version!r}")

    operations = {(path, method) for path, method, _ in operation_items(schema)}
    for path, method in sorted(REQUIRED_RUNTIME_OPERATIONS - operations):
        issues.append(f"missing required AgentScope gateway operation {method.upper()} {path}")
    for path in sorted(_removed_paths_present(schema)):
        issues.append(f"removed compatibility path is still exposed: {path}")

    for path, method, operation in operation_items(schema):
        responses = _responses(operation)
        for status_code in sorted(expected_error_statuses(path, method, operation)):
            if str(status_code) not in responses:
                issues.append(f"{method.upper()} {path} missing documented {status_code} response")
        if path.startswith("/api/") and not operation.get("security"):
            issues.append(f"{method.upper()} {path} missing Bearer security declaration")
        issues.extend(_audit_empty_success_schema(path, method, responses))

    stream = _operation(schema, RUNTIME_STREAM_PATH, "get")
    stream_content = _success_content(stream)
    if set(stream_content) != {"text/event-stream"}:
        issues.append(f"GET {RUNTIME_STREAM_PATH} must expose only text/event-stream")
    if stream.get("x-agentgov-sse-contract") != runtime_sse_contract():
        issues.append(f"GET {RUNTIME_STREAM_PATH} does not declare the native pass-through contract")

    for (path, method), expected_status in NON_200_SUCCESS_CODES.items():
        operation = _operation(schema, path, method)
        if not operation:
            continue
        success = {status for status in _responses(operation) if status.startswith("2")}
        if success != {expected_status}:
            issues.append(f"{method.upper()} {path} success statuses {sorted(success)} != [{expected_status!r}]")
    return issues


def audit_live_matches_local(live_schema: OpenApiObject, local_schema: OpenApiObject) -> list[str]:
    live = _canonical_schema(live_schema)
    local = _canonical_schema(local_schema)
    return [
        f"live/local semantic diff at {pointer}: local={expected!r} live={actual!r}" for pointer, expected, actual in _json_differences(local, live, limit=100)
    ]


def _removed_paths_present(schema: OpenApiObject) -> set[str]:
    paths = set(_paths(schema))
    found = paths & set(REMOVED_RUNTIME_PATHS)
    found.update(
        path
        for path in paths
        if path.startswith(
            (
                "/api/sessions/",
                "/api/claude-user-input-requests/",
                "/api/settings/openai-compat-agent/",
                "/v1/responses/",
                "/v1/conversations/",
                "/v1/agentgov/confirmation-requests/",
            )
        )
    )
    return found


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
            issues.append(f"{method.upper()} {path} {status_code} documents an empty JSON schema")
    return issues


def _success_content(operation: OpenApiObject) -> OpenApiObject:
    success = _responses(operation).get("200", {})
    content = success.get("content") if isinstance(success, dict) else None
    return content if isinstance(content, dict) else {}


def _responses(operation: object) -> OpenApiObject:
    responses = operation.get("responses") if isinstance(operation, dict) else None
    return responses if isinstance(responses, dict) else {}


def _paths(schema: OpenApiObject) -> OpenApiObject:
    paths = schema.get("paths")
    return paths if isinstance(paths, dict) else {}


def _operation(schema: OpenApiObject, path: str, method: str) -> OpenApiObject:
    path_item = _paths(schema).get(path)
    operation = path_item.get(method) if isinstance(path_item, dict) else None
    return operation if isinstance(operation, dict) else {}


def _info_version(schema: OpenApiObject) -> str | None:
    info = schema.get("info")
    value = info.get("version") if isinstance(info, dict) else None
    return value if isinstance(value, str) else None


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
    parser = argparse.ArgumentParser(description="Audit the AgentGov AgentScope gateway OpenAPI contract.")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--compare-local", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()
    if args.input and args.base_url:
        parser.error("--input and --base-url are mutually exclusive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
