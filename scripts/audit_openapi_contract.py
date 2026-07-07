from __future__ import annotations

# ruff: noqa: E402
import argparse
import json
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.openapi_contract import CHAT_STREAM_PATH, RESPONSES_PATH, expected_error_statuses, operation_items
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
    print(f"openapi contract OK: version={schema.get('info', {}).get('version')} operations={len(operation_items(schema))}")
    return 0


def audit_schema(schema: OpenApiObject, *, expected_version: str | None = None) -> list[str]:
    issues: list[str] = []
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
    issues.extend(_audit_streaming_media_types(schema))
    return issues


def audit_live_matches_local(live_schema: OpenApiObject, local_schema: OpenApiObject) -> list[str]:
    live_ops = {(path, method) for path, method, _ in operation_items(live_schema)}
    local_ops = {(path, method) for path, method, _ in operation_items(local_schema)}
    issues: list[str] = []
    for path, method in sorted(local_ops - live_ops):
        issues.append(f"live schema missing local operation {method.upper()} {path}")
    for path, method in sorted(live_ops - local_ops):
        issues.append(f"live schema exposes operation absent locally {method.upper()} {path}")
    local_version = _info_version(local_schema)
    live_version = _info_version(live_schema)
    if live_version != local_version:
        issues.append(f"live info.version {live_version!r} != local info.version {local_version!r}")
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
    paths = schema.get("paths", {})
    if not isinstance(paths, dict):
        return {}
    operation = paths.get(path, {}).get(method, {}) if isinstance(paths.get(path), dict) else {}
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
    parser.add_argument("--compare-local", action="store_true", help="When using --base-url, compare live paths/version to local export.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--fail", action="store_true", help="Exit non-zero when contract issues are found.")
    args = parser.parse_args()
    if args.input and args.base_url:
        parser.error("--input and --base-url are mutually exclusive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
