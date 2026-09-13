#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime.integrations.runtime_langfuse import project_validation_trace

from scripts.agentscope_live_acceptance_scenarios import (
    LiveAcceptanceError,
    load_scenarios,
)
from scripts.container_acceptance_inputs import AcceptanceError, require_live_authorization

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
_DIRECT_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RedisCommandError(RuntimeError):
    """表示 Redis 队列真实查询未成功，不得投影为空队列。"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-check local Langfuse observability.")
    parser.add_argument("--env-file", default="docker/.env")
    parser.add_argument("--scenario-file", type=Path)
    parser.add_argument("--agent-id")
    parser.add_argument(
        "--projected-trace-id",
        help="只用私有 Langfuse 凭据核对一个精确 trace；不触发 Runtime run，也不输出 trace 正文。",
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    env = load_env(Path(args.env_file))
    try:
        require_live_authorization(
            dict(env),
            require_trace_complete=True,
        )
    except AcceptanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.projected_trace_id:
        if args.scenario_file is not None or args.agent_id:
            parser.error("--projected-trace-id 不能与 --scenario-file/--agent-id 同时使用")
        return check_projected_trace(
            env,
            trace_id=args.projected_trace_id,
            timeout_seconds=args.timeout_seconds,
        )
    if args.scenario_file is None or not args.agent_id:
        parser.error("Runtime smoke requires --scenario-file and --agent-id")
    try:
        reviewed = load_scenarios(
            args.scenario_file,
            expected_agent_id=args.agent_id,
        )
        scenario = next(item for item in reviewed.scenarios if item.purpose == "success")
    except StopIteration:
        print("ERROR: Langfuse smoke requires one reviewed success scenario", file=sys.stderr)
        return 2
    except LiveAcceptanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    langfuse_url = resolve_langfuse_url(env)
    errors: list[str] = []

    health_url = f"{langfuse_url.rstrip('/')}/api/public/health"
    if wait_for_health(health_url, args.timeout_seconds):
        print(f"Langfuse health OK: {health_url}")
    else:
        errors.append(f"Langfuse health failed: {health_url}")

    print_runtime_versions(env)
    errors.extend(
        trigger_and_check_runtime_trace(
            env,
            governance_agent_id=args.agent_id,
            scenario_id=scenario.scenario_id,
            input_text=scenario.input_text,
            timeout_seconds=args.timeout_seconds,
        ),
    )
    errors.extend(check_queues(env))

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


def check_projected_trace(
    env: Mapping[str, str],
    *,
    trace_id: str,
    timeout_seconds: int,
) -> int:
    """轮询一个真实 Langfuse trace，仅输出身份与投影可用状态。"""

    if re.fullmatch(r"[0-9a-f]{32}", trace_id) is None:
        print("ERROR: projected trace id must be 32 lowercase hex characters", file=sys.stderr)
        return 2
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_error = "trace not available"
    while time.monotonic() <= deadline:
        try:
            trace = fetch_projected_trace(env, trace_id)
            observed = string_value(trace, "id") or string_value(trace, "trace_id")
            if observed == trace_id and trace.get("fetch_status") != "failed":
                print(f"Langfuse projected trace OK: {trace_id}")
                return 0
            last_error = "projected identity unavailable"
        except Exception as exc:
            last_error = exc.__class__.__name__
        time.sleep(2)
    print(f"ERROR: projected trace unavailable before timeout ({last_error})", file=sys.stderr)
    return 1


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
    container_error = redis_container_status_error(redis_container, running=container_running(redis_container))
    if container_error:
        return [container_error]
    auth = env.get("LANGFUSE_REDIS_AUTH")
    if not auth:
        return ["Langfuse Redis queue check requires private LANGFUSE_REDIS_AUTH"]
    errors: list[str] = []
    print("Langfuse queue state:")
    try:
        for queue in QUEUE_NAMES:
            counts = {state: redis_queue_count(redis_container, auth, queue, state) for state in QUEUE_STATES}
            print("  " + queue + ": " + ", ".join(f"{state}={counts[state]}" for state in QUEUE_STATES))
            if counts["failed"] > 0:
                errors.append(f"{queue} has failed jobs: {counts['failed']}")
            if queue in {"otel-ingestion-queue", "secondary-otel-ingestion-queue"}:
                backlog = counts["wait"] + counts["delayed"]
                if backlog > 0:
                    errors.append(f"{queue} has pending ingestion backlog: {backlog}")
    except RedisCommandError as exc:
        errors.append(str(exc))
    return errors


def redis_container_status_error(container: str, *, running: bool) -> str | None:
    if running:
        return None
    return f"Langfuse Redis queue check failed: container {container} is not running"


def trigger_and_check_runtime_trace(
    env: Mapping[str, str],
    *,
    governance_agent_id: str,
    scenario_id: str,
    input_text: str,
    timeout_seconds: int,
) -> list[str]:
    """通过 AgentGov 公共 API 触发本轮 run，并只验收该 run 的语义 Trace。"""

    api_base = (env.get("API_BASE") or f"http://localhost:{env.get('HOST_PORT') or '50400'}").rstrip("/")
    api_key = env.get("API_KEY") or ""
    runtime_agent_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    deadline = time.monotonic() + max(1, timeout_seconds)

    try:
        runtime_agent_id, session_id = _open_smoke_session(
            api_base,
            api_key,
            governance_agent_id,
            scenario_id,
        )
        run_id = _start_smoke_run(
            api_base,
            api_key,
            runtime_agent_id,
            session_id,
            scenario_id=scenario_id,
            input_text=input_text,
        )
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
            env=env,
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


def _open_smoke_session(
    api_base: str,
    api_key: str,
    governance_agent_id: str,
    scenario_id: str,
) -> tuple[str, str]:
    current, _ = request_agentgov_json(
        f"{api_base}/api/runtime/agents/{quote(governance_agent_id, safe='')}/current",
        method="GET",
        api_key=api_key,
    )
    runtime_agent_id = string_value(current, "runtime_agent_id")
    if not runtime_agent_id:
        raise RuntimeError("AgentGov did not return runtime_agent_id for Langfuse smoke")
    session, _ = request_agentgov_json(
        f"{api_base}/api/runtime/sessions/",
        method="POST",
        payload={
            "agent_id": runtime_agent_id,
            "name": f"langfuse-smoke-{scenario_id[:40]}-{uuid4().hex[:8]}",
        },
        api_key=api_key,
        extra_headers={"Idempotency-Key": f"langfuse-smoke-session-{uuid4().hex}"},
    )
    session_id = string_value(session, "session_id")
    if not session_id:
        raise RuntimeError("AgentGov did not return session_id for Langfuse smoke")
    return runtime_agent_id, session_id


def _start_smoke_run(
    api_base: str,
    api_key: str,
    runtime_agent_id: str,
    session_id: str,
    *,
    scenario_id: str,
    input_text: str,
) -> str:
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
                "content": [{"type": "text", "text": input_text}],
            },
            "metadata": {
                "purpose": "langfuse-smoke",
                "scenario_id": scenario_id,
            },
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
    env: Mapping[str, str],
    api_base: str,
    api_key: str,
    run_id: str,
    expected_reply_ids: frozenset[str],
    deadline: float,
) -> list[str]:
    last_reason = "trace not returned"
    while time.monotonic() <= deadline:
        payload, _ = request_agentgov_json(f"{api_base}/api/agent-runs/{run_id}/trace", api_key=api_key)
        trace_id = string_value(payload, "trace_id")
        trace_status = string_value(payload, "trace_status")
        if trace_id and trace_status == "complete":
            try:
                trace = fetch_projected_trace(env, trace_id)
            except Exception as exc:
                last_reason = f"Langfuse query pending: {exc.__class__.__name__}"
                time.sleep(2)
                continue
            observations = list_value(trace, "observations")
            names = {str(item.get("name") or "") for item in observations}
            root_observation = next(
                (item for item in observations if item.get("name") == "agentgov.run"),
                {},
            )
            root_attribute_keys = metadata_attribute_keys(root_observation)
            stage_reply_ids = [
                _single_observation_attribute(item, "agentscope.agent.reply_id") for item in observations if item.get("name") == "agentgov.run.stage"
            ]
            error_observation_names = {str(item.get("name") or "") for item in observations if str(item.get("level") or "").upper() == "ERROR"}
            trace_name = str(trace.get("name") or "")
            errors = runtime_trace_observation_errors(
                trace_id=trace_id,
                projected_trace_id=string_value(trace, "id") or string_value(trace, "trace_id"),
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
            last_reason = str(payload.get("trace_status") or "trace pending")
        time.sleep(2)
    return [f"AgentScope run {run_id} trace was not complete before timeout: {last_reason}"]


def fetch_projected_trace(env: Mapping[str, str], trace_id: str) -> ExternalJsonObject:
    """使用私有只读凭据取回严格投影视图，不经公开 AgentGov payload 暴露 observations。"""

    public_key = env.get("LANGFUSE_PUBLIC_KEY") or ""
    secret_key = env.get("LANGFUSE_SECRET_KEY") or ""
    if not public_key or not secret_key:
        raise RuntimeError("Langfuse query credentials are missing")
    from langfuse.api.client import LangfuseAPI

    with httpx.Client(timeout=10, trust_env=False) as http_client:
        client = LangfuseAPI(
            base_url=resolve_langfuse_url(env),
            username=public_key,
            password=secret_key,
            x_langfuse_public_key=public_key,
            timeout=10,
            httpx_client=http_client,
        )
        value = project_validation_trace(
            client.trace.get(trace_id, fields="core,observations"),
        )
    observed_trace_id = string_value(value, "id") or string_value(value, "trace_id")
    if observed_trace_id != trace_id:
        raise RuntimeError("Langfuse returned a different trace identity")
    return value


def runtime_trace_observation_errors(
    *,
    trace_id: str,
    projected_trace_id: str | None,
    trace_name: str,
    names: set[str],
    root_attribute_keys: set[str] | frozenset[str] = frozenset(),
    expected_reply_ids: set[str] | frozenset[str] = frozenset(),
    stage_reply_ids: list[str | None] | tuple[str | None, ...] = (),
    error_observation_names: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    errors: list[str] = []
    if projected_trace_id != trace_id:
        errors.append(f"trace {trace_id} query returned a different trace identity")
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
    with _DIRECT_HTTP.open(request, timeout=10) as response:
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
    with _DIRECT_HTTP.open(request, timeout=10) as response:
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
    if key_type == "none":
        return 0
    cardinality_command = {"list": "llen", "zset": "zcard", "set": "scard"}.get(key_type)
    if cardinality_command is None:
        raise RedisCommandError(f"Langfuse Redis queue check returned unexpected key type: {key_type or 'empty'}")
    return parse_redis_queue_count(key_type, redis(container, auth, cardinality_command, key))


def parse_redis_queue_count(key_type: str, count_output: str) -> int:
    """仅 `type=none` 表示 key 不存在；空输出或非整数都是查询失败。"""

    if key_type == "none":
        return 0
    if key_type not in {"list", "zset", "set"}:
        raise RedisCommandError(f"Langfuse Redis queue check returned unexpected key type: {key_type or 'empty'}")
    try:
        count = int(count_output)
    except (TypeError, ValueError) as exc:
        raise RedisCommandError("Langfuse Redis queue cardinality was not returned") from exc
    if count < 0:
        raise RedisCommandError("Langfuse Redis queue cardinality was negative")
    return count


def redis(container: str, auth: str, *args: str) -> str:
    operation = args[0] if args else "unknown"
    try:
        # 不把 Redis 密码放进宿主机可观察的 docker exec argv。固定 shell
        # 从 stdin 读取秘密后才在容器内设置 redis-cli 专用环境变量。
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "sh",
                "-eu",
                "-c",
                'REDISCLI_AUTH="$(cat)"; export REDISCLI_AUTH; exec redis-cli --no-auth-warning "$@"',
                "redis-cli",
                *args,
            ],
            input=auth,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RedisCommandError(f"Langfuse Redis {operation} command could not be executed") from exc
    return require_redis_command_output(result, operation)


def require_redis_command_output(result: subprocess.CompletedProcess[str], operation: str) -> str:
    if result.returncode != 0:
        raise RedisCommandError(f"Langfuse Redis {operation} command failed with exit code {result.returncode}")
    return result.stdout.strip()


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
