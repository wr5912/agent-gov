#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

QUEUE_NAMES = (
    "otel-ingestion-queue",
    "secondary-otel-ingestion-queue",
    "trace-upsert",
    "ingestion-queue",
)
QUEUE_STATES = ("wait", "active", "delayed", "failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-check local Langfuse observability.")
    parser.add_argument("--env-file", default="docker/.env")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    env = load_env(Path(args.env_file))
    langfuse_url = resolve_langfuse_url(env)
    errors: list[str] = []

    health_url = f"{langfuse_url.rstrip('/')}/api/public/health"
    if wait_for_health(health_url, args.timeout_seconds):
        print(f"Langfuse health OK: {health_url}")
    else:
        errors.append(f"Langfuse health failed: {health_url}")

    print_runtime_versions(env)
    errors.extend(check_queues(env))
    errors.extend(check_latest_trace(env, langfuse_url))

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


# Read-only view for external API JSON responses; not a Runtime JsonObject contract.
ExternalJsonObject = Mapping[str, object]


def load_env(path: Path) -> Mapping[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    merged = dict(values)
    merged.update({key: value for key, value in os.environ.items() if value})
    return merged


def resolve_langfuse_url(env: Mapping[str, str]) -> str:
    if env.get("LANGFUSE_NEXTAUTH_URL"):
        return env["LANGFUSE_NEXTAUTH_URL"]
    port = env.get("LANGFUSE_HOST_PORT") or "53000"
    return f"http://localhost:{port}"


def wait_for_health(url: str, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        try:
            get_json(url)
            return True
        except Exception:
            time.sleep(2)
    return False


def print_runtime_versions(env: Mapping[str, str]) -> None:
    api_base = env.get("API_BASE") or f"http://localhost:{env.get('HOST_PORT') or '58080'}"
    try:
        payload = get_json(f"{api_base.rstrip('/')}/health")
    except Exception as exc:
        print(f"Runtime health skipped: {exc}")
        return
    versions = mapping_value(payload, "runtime_dependency_versions")
    if not versions:
        print("Runtime dependency versions unavailable")
        return
    print("Runtime dependency versions:")
    for key in sorted(versions):
        value = versions.get(key)
        if value:
            print(f"  {key}: {value}")


def check_queues(env: Mapping[str, str]) -> list[str]:
    container_prefix = env.get("CONTAINER_NAME_PREFIX") or "agent-gov-hitl"
    redis_container = env.get("LANGFUSE_REDIS_CONTAINER") or f"{container_prefix}-langfuse-redis"
    if not container_running(redis_container):
        print(f"Langfuse Redis queue check skipped: container {redis_container} is not running")
        return []
    auth = env.get("LANGFUSE_REDIS_AUTH") or "langfuse-redis"
    errors: list[str] = []
    print("Langfuse queue state:")
    for queue in QUEUE_NAMES:
        counts = {state: redis_queue_count(redis_container, auth, queue, state) for state in QUEUE_STATES}
        print("  " + queue + ": " + ", ".join(f"{state}={counts[state]}" for state in QUEUE_STATES))
        if counts["failed"] > 0:
            errors.append(f"{queue} has failed jobs: {counts['failed']}")
        if queue in {"otel-ingestion-queue", "secondary-otel-ingestion-queue"}:
            backlog = counts["wait"] + counts["delayed"]
            if backlog > 0:
                errors.append(f"{queue} has pending ingestion backlog: {backlog}")
    return errors


def check_latest_trace(env: Mapping[str, str], langfuse_url: str) -> list[str]:
    public_key = env.get("LANGFUSE_PUBLIC_KEY") or env.get("LANGFUSE_INIT_PROJECT_PUBLIC_KEY")
    secret_key = env.get("LANGFUSE_SECRET_KEY") or env.get("LANGFUSE_INIT_PROJECT_SECRET_KEY")
    if not public_key or not secret_key:
        print("Langfuse trace check skipped: public/secret key not configured")
        return []

    auth = (public_key, secret_key)
    traces = list_value(get_json(f"{langfuse_url.rstrip('/')}/api/public/traces?limit=20", auth=auth), "data")
    runtime_trace = next((item for item in traces if str(item.get("name") or "").startswith("runtime.")), None)
    if not runtime_trace:
        print("Langfuse trace check skipped: no runtime.* trace found yet")
        return []

    trace_id = runtime_trace.get("id")
    trace = get_json(
        f"{langfuse_url.rstrip('/')}/api/public/traces/{trace_id}?fields=core,io,observations,metrics",
        auth=auth,
    )
    observations = list_value(trace, "observations")
    if not observations:
        observations = list_value(
            get_json(
                f"{langfuse_url.rstrip('/')}/api/public/observations?traceId={trace_id}&limit=100",
                auth=auth,
            ),
            "data",
        )

    names = {str(item.get("name") or "") for item in observations}
    trace_name = str(runtime_trace.get("name") or "")
    print(f"Langfuse latest runtime trace: {trace_id} ({trace_name})")
    print(f"  observations={len(observations)}")

    errors: list[str] = []
    if trace_name.startswith("runtime.output_formatter."):
        errors.append(f"trace {trace_id} is named after formatter child span instead of runtime root")
    if not any(name.startswith("runtime.") for name in names):
        errors.append(f"trace {trace_id} does not include a runtime root observation")
    if trace_name.startswith("runtime.governor.") and trace_name not in names:
        errors.append(f"governor trace {trace_id} is missing matching root observation {trace_name}")
    if not any(name.startswith("claude_code.") for name in names):
        errors.append(f"trace {trace_id} does not include Claude Code observations")
    if trace.get("input") is None:
        errors.append(f"trace {trace_id} is missing root input")
    if trace.get("output") is None:
        errors.append(f"trace {trace_id} is missing root output")
    return errors


def mapping_value(payload: ExternalJsonObject, key: str) -> ExternalJsonObject:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def list_value(payload: ExternalJsonObject, key: str) -> list[ExternalJsonObject]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def get_json(url: str, auth: tuple[str, str] | None = None) -> ExternalJsonObject:
    request = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def container_running(container: str) -> bool:
    result = run(["docker", "inspect", "-f", "{{.State.Running}}", container])
    return result.returncode == 0 and result.stdout.strip() == "true"


def redis_queue_count(container: str, auth: str, queue: str, state: str) -> int:
    key = f"bull:{queue}:{state}"
    key_type = redis(container, auth, "type", key)
    if key_type == "list":
        return int(redis(container, auth, "llen", key) or "0")
    if key_type == "zset":
        return int(redis(container, auth, "zcard", key) or "0")
    if key_type == "set":
        return int(redis(container, auth, "scard", key) or "0")
    return 0


def redis(container: str, auth: str, *args: str) -> str:
    result = run(
        [
            "docker",
            "exec",
            container,
            "redis-cli",
            "-a",
            auth,
            "--no-auth-warning",
            *args,
        ]
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
