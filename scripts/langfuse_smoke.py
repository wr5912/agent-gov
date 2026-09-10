#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

QUEUE_NAMES = (
    "otel-ingestion-queue",
    "secondary-otel-ingestion-queue",
    "trace-upsert",
    "ingestion-queue",
)
QUEUE_STATES = ("wait", "active", "delayed", "failed")
TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}
REQUIRED_ROOT_ATTRIBUTES = frozenset(
    {
        "agentgov.run.id",
        "agentgov.agent.id",
        "agentgov.agent.version_id",
        "agentgov.harness.digest",
        "agentgov.runtime.version",
        "agentscope.agent.id",
        "agentscope.runtime.version",
        "agentscope.session.id",
        "agentgov.run.finished_reason",
    }
)


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
    errors.extend(trigger_and_check_runtime_trace(env, timeout_seconds=args.timeout_seconds))
    errors.extend(check_queues(env))

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
    port = env.get("LANGFUSE_HOST_PORT") or "50402"
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
    api_base = env.get("API_BASE") or f"http://localhost:{env.get('HOST_PORT') or '50400'}"
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
    container_prefix = env.get("CONTAINER_NAME_PREFIX") or "agent-gov"
    redis_container = env.get("LANGFUSE_REDIS_CONTAINER") or f"{container_prefix}-langfuse-redis"
    if not container_running(redis_container):
        print(f"Langfuse Redis queue check skipped: container {redis_container} is not running")
        return []
    auth = env.get("LANGFUSE_REDIS_AUTH")
    if not auth:
        return ["Langfuse Redis queue check requires private LANGFUSE_REDIS_AUTH"]
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


def trigger_and_check_runtime_trace(env: Mapping[str, str], *, timeout_seconds: int) -> list[str]:
    """通过 AgentGov 公共 API 触发本轮 run，并只验收该 run 的语义 Trace。"""

    api_base = (env.get("API_BASE") or f"http://localhost:{env.get('HOST_PORT') or '50400'}").rstrip("/")
    api_key = env.get("API_KEY") or ""
    governance_agent_id = env.get("LANGFUSE_SMOKE_AGENT_ID") or "security-operations-expert"
    runtime_agent_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    deadline = time.monotonic() + max(1, timeout_seconds)

    try:
        runtime_agent_id, session_id = _open_smoke_session(api_base, api_key, governance_agent_id)
        run_id = _start_smoke_run(api_base, api_key, runtime_agent_id, session_id)
        print(f"AgentScope smoke run started: run_id={run_id} session_id={session_id}")

        terminal = wait_for_terminal_run(
            api_base=api_base,
            api_key=api_key,
            run_id=run_id,
            deadline=deadline,
        )
        terminal_status = string_value(terminal, "status")
        if terminal_status != "succeeded":
            return [f"AgentScope smoke run {run_id} ended with status={terminal_status or 'unknown'}"]

        return wait_for_semantic_trace(
            api_base=api_base,
            api_key=api_key,
            run_id=run_id,
            expected_reply_ids=frozenset(string_list_value(terminal, "reply_ids")),
            deadline=deadline,
        )
    except Exception as exc:
        return [f"AgentScope Langfuse smoke failed: {exc.__class__.__name__}: {exc}"]
    finally:
        _cleanup_smoke_run(api_base, api_key, runtime_agent_id, session_id, run_id)


def _open_smoke_session(api_base: str, api_key: str, governance_agent_id: str) -> tuple[str, str]:
    current, _ = request_agentgov_json(
        f"{api_base}/api/runtime/agents/{quote(governance_agent_id, safe='')}/provision",
        method="POST",
        payload={},
        api_key=api_key,
    )
    runtime_agent_id = string_value(current, "runtime_agent_id")
    if not runtime_agent_id:
        raise RuntimeError("AgentGov did not return runtime_agent_id for Langfuse smoke")
    session, _ = request_agentgov_json(
        f"{api_base}/api/runtime/sessions/",
        method="POST",
        payload={"agent_id": runtime_agent_id, "name": f"langfuse-smoke-{uuid4().hex[:12]}"},
        api_key=api_key,
        extra_headers={"Idempotency-Key": f"langfuse-smoke-session-{uuid4().hex}"},
    )
    session_id = string_value(session, "session_id")
    if not session_id:
        raise RuntimeError("AgentGov did not return session_id for Langfuse smoke")
    return runtime_agent_id, session_id


def _start_smoke_run(api_base: str, api_key: str, runtime_agent_id: str, session_id: str) -> str:
    _, headers = request_agentgov_json(
        f"{api_base}/api/runtime/chat/",
        method="POST",
        payload={
            "agent_id": runtime_agent_id,
            "session_id": session_id,
            "client_operation_id": f"langfuse-smoke-turn-{uuid4().hex}",
            "input": {
                "name": "user",
                "role": "user",
                "content": [{"type": "text", "text": "Reply with exactly AGENTGOV_LANGFUSE_SMOKE_OK."}],
            },
            "metadata": {"purpose": "langfuse-smoke"},
        },
        api_key=api_key,
    )
    run_id = header_value(headers, "X-AgentGov-Run-Id")
    if not run_id:
        raise RuntimeError("AgentGov did not return X-AgentGov-Run-Id for Langfuse smoke")
    return run_id


def _cleanup_smoke_run(
    api_base: str,
    api_key: str,
    runtime_agent_id: str | None,
    session_id: str | None,
    run_id: str | None,
) -> None:
    if run_id:
        with suppress(Exception):
            current, _ = request_agentgov_json(f"{api_base}/api/agent-runs/{run_id}", api_key=api_key)
            if string_value(current, "status") not in TERMINAL_RUN_STATUSES:
                request_agentgov_json(
                    f"{api_base}/api/agent-runs/{run_id}/cancel",
                    method="POST",
                    payload={},
                    api_key=api_key,
                )
    if session_id and runtime_agent_id:
        with suppress(Exception):
            request_agentgov_json(
                f"{api_base}/api/runtime/sessions/{quote(session_id, safe='')}?agent_id={quote(runtime_agent_id, safe='')}",
                method="DELETE",
                api_key=api_key,
            )


def wait_for_terminal_run(
    *,
    api_base: str,
    api_key: str,
    run_id: str,
    deadline: float,
) -> ExternalJsonObject:
    latest: ExternalJsonObject = {}
    while time.monotonic() <= deadline:
        latest, _ = request_agentgov_json(f"{api_base}/api/agent-runs/{run_id}", api_key=api_key)
        status = string_value(latest, "status")
        if status in TERMINAL_RUN_STATUSES:
            return latest
        time.sleep(1)
    raise TimeoutError(f"AgentScope run {run_id} did not reach terminal state")


def wait_for_semantic_trace(
    *,
    api_base: str,
    api_key: str,
    run_id: str,
    expected_reply_ids: frozenset[str],
    deadline: float,
) -> list[str]:
    last_reason = "trace not returned"
    while time.monotonic() <= deadline:
        payload, _ = request_agentgov_json(f"{api_base}/api/agent-runs/{run_id}/trace", api_key=api_key)
        trace = mapping_value(payload, "trace")
        trace_id = string_value(payload, "trace_id")
        trace_status = string_value(payload, "trace_status")
        if trace and trace.get("fetch_status") != "failed" and trace_id and trace_status == "complete":
            observations = list_value(trace, "observations")
            names = {str(item.get("name") or "") for item in observations}
            root_observation = next(
                (item for item in observations if item.get("name") == "agentgov.run"),
                {},
            )
            root_attribute_keys = metadata_attribute_keys(root_observation.get("metadata"))
            stage_reply_ids = [
                _single_observation_attribute(item, "agentscope.agent.reply_id") for item in observations if item.get("name") == "agentgov.run.stage"
            ]
            error_observation_names = {str(item.get("name") or "") for item in observations if str(item.get("level") or "").upper() == "ERROR"}
            trace_name = str(trace.get("name") or "")
            errors = runtime_trace_observation_errors(
                trace_id=trace_id,
                trace_name=trace_name,
                names=names,
                root_attribute_keys=root_attribute_keys,
                expected_reply_ids=expected_reply_ids,
                stage_reply_ids=stage_reply_ids,
                error_observation_names=error_observation_names,
            )
            if not errors:
                print(f"Langfuse AgentScope trace OK: {trace_id} ({trace_name}) observations={len(observations)}")
                return []
            last_reason = "; ".join(errors)
        else:
            last_reason = str(trace.get("error") or payload.get("trace_status") or "trace pending")
        time.sleep(2)
    return [f"AgentScope run {run_id} trace was not complete before timeout: {last_reason}"]


def runtime_trace_observation_errors(
    *,
    trace_id: str,
    trace_name: str,
    names: set[str],
    root_attribute_keys: set[str] | frozenset[str] = frozenset(),
    expected_reply_ids: set[str] | frozenset[str] = frozenset(),
    stage_reply_ids: list[str | None] | tuple[str | None, ...] = (),
    error_observation_names: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    errors: list[str] = []
    if trace_name != "agentgov.run":
        errors.append(f"trace {trace_id} root is not the AgentGov run span")
    if "agentgov.run" not in names:
        errors.append(f"trace {trace_id} does not include the AgentGov run observation")
    if "agentgov.run.stage" not in names:
        errors.append(f"trace {trace_id} does not include an AgentGov run stage")
    if "invoke_agent" not in names:
        errors.append(f"trace {trace_id} does not include the redacted AgentScope agent observation")
    if "chat" not in names:
        errors.append(f"trace {trace_id} does not include the redacted AgentScope model observation")
    missing_attributes = sorted(REQUIRED_ROOT_ATTRIBUTES - root_attribute_keys)
    if missing_attributes:
        errors.append(f"trace {trace_id} run observation is missing attributes: " + ", ".join(missing_attributes))
    observed_reply_ids = {reply_id for reply_id in stage_reply_ids if reply_id}
    if not expected_reply_ids:
        errors.append(f"trace {trace_id} run API returned no persisted reply_ids")
    elif any(reply_id is None for reply_id in stage_reply_ids) or observed_reply_ids != set(expected_reply_ids):
        errors.append(f"trace {trace_id} run stages do not exactly match persisted reply_ids")
    if error_observation_names:
        errors.append(f"trace {trace_id} includes error observations: " + ", ".join(sorted(error_observation_names)))
    return errors


def metadata_attribute_keys(value: object) -> set[str]:
    """Collect exact OTel attribute keys from Langfuse's JSON-safe metadata wrappers."""

    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(metadata_attribute_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(metadata_attribute_keys(item))
    return keys


def _single_observation_attribute(observation: ExternalJsonObject, key: str) -> str | None:
    values: list[str] = []
    direct = observation.get(key)
    if isinstance(direct, str) and direct:
        values.append(direct)
    for container_name in ("attributes", "metadata"):
        values.extend(_metadata_attribute_values(observation.get(container_name), key))
    unique = set(values)
    return unique.pop() if len(unique) == 1 else None


def _metadata_attribute_values(value: object, key: str) -> list[str]:
    values: list[str] = []
    if isinstance(value, Mapping):
        for candidate_key, item in value.items():
            if str(candidate_key) == key and isinstance(item, str) and item:
                values.append(item)
            values.extend(_metadata_attribute_values(item, key))
    elif isinstance(value, list):
        for item in value:
            values.extend(_metadata_attribute_values(item, key))
    return values


def mapping_value(payload: ExternalJsonObject, key: str) -> ExternalJsonObject:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def list_value(payload: ExternalJsonObject, key: str) -> list[ExternalJsonObject]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def string_list_value(payload: ExternalJsonObject, key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def string_value(payload: ExternalJsonObject, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def header_value(headers: Mapping[str, str], key: str) -> str | None:
    expected = key.casefold()
    for name, value in headers.items():
        if name.casefold() == expected and value:
            return value
    return None


def get_json(url: str) -> ExternalJsonObject:
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def request_agentgov_json(
    url: str,
    *,
    method: str = "GET",
    payload: Mapping[str, object] | None = None,
    api_key: str = "",
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[ExternalJsonObject, Mapping[str, str]]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "User-Agent": "agent-gov-langfuse-smoke"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        response_headers = {str(name): str(value) for name, value in response.headers.items()}
    if not raw:
        return {}, response_headers
    parsed = json.loads(raw.decode("utf-8"))
    return (parsed if isinstance(parsed, dict) else {}), response_headers


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
