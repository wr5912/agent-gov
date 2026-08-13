from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import TypeAlias
from urllib.error import HTTPError
from urllib.request import Request, urlopen

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

_READINESS_STATUSES = frozenset({"not_checked", "checking", "ready", "degraded"})
_READINESS_ERROR_CODES = frozenset(
    {
        "LITELLM_CLAUDE_CODE_COMPAT_FAILED",
        "MODEL_AGENT_LOOP_CAPABILITY_FAILED",
        "MODEL_PROVIDER_CONFIGURATION_MISSING",
        "MODEL_PROVIDER_NOT_CHECKED",
        "MODEL_PROVIDER_PROBE_IN_PROGRESS",
        "MODEL_PROVIDER_READINESS_PROBE_FAILED",
        "MODEL_PROVIDER_SIDECAR_UNAVAILABLE",
        "MODEL_SCHEMA_EXACT_OUTPUT_FAILED",
        "READINESS_RESPONSE_UNAVAILABLE",
        "VLLM_BASE_URL_INVALID",
        "VLLM_CHAT_PROBE_FAILED",
        "VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED",
        "VLLM_MODELS_PROBE_FAILED",
        "VLLM_TOOL_CALLING_UNSUPPORTED",
        "VLLM_VERSION_PROBE_FAILED",
    }
)
_READINESS_REASONS = frozenset(
    {
        "connection_error",
        "invalid_json",
        "invalid_version",
        "missing_provider_configuration",
        "missing_provider_endpoint",
        "missing_route_endpoint",
        "missing_version",
        "request_failed",
        "timeout",
        "vllm_base_url_must_not_end_in_v1",
    }
)
_READINESS_PROBES = frozenset(
    {
        "agent_runtime_capabilities",
        "agent_tool_loop",
        "chat",
        "configuration",
        "models",
        "provider_route",
        "schema_exact_json",
        "sidecar_readiness",
        "tool_calling",
        "vllm_version",
    }
)
_READINESS_ACTIONS = {
    "LITELLM_CLAUDE_CODE_COMPAT_FAILED": "verify_provider_compatibility",
    "MODEL_AGENT_LOOP_CAPABILITY_FAILED": "verify_agent_loop",
    "MODEL_PROVIDER_CONFIGURATION_MISSING": "configure_provider",
    "MODEL_PROVIDER_NOT_CHECKED": "wait_for_provider_probe",
    "MODEL_PROVIDER_PROBE_IN_PROGRESS": "wait_for_provider_probe",
    "MODEL_PROVIDER_READINESS_PROBE_FAILED": "retry_provider_probe",
    "MODEL_PROVIDER_SIDECAR_UNAVAILABLE": "verify_sidecar",
    "MODEL_SCHEMA_EXACT_OUTPUT_FAILED": "verify_schema_output",
    "READINESS_RESPONSE_UNAVAILABLE": "retry_readiness_request",
    "VLLM_BASE_URL_INVALID": "fix_provider_base_url",
    "VLLM_CHAT_PROBE_FAILED": "verify_provider_capability",
    "VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED": "verify_provider_compatibility",
    "VLLM_MODELS_PROBE_FAILED": "verify_provider_capability",
    "VLLM_TOOL_CALLING_UNSUPPORTED": "verify_provider_capability",
    "VLLM_VERSION_PROBE_FAILED": "verify_external_vllm",
}


def _env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return value.split(" #", 1)[0].strip().strip('"')
    return None


def _get_json(url: str, *, timeout: float) -> tuple[int | None, JsonObject | None, str | None]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "agent-gov-health-diagnose"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = response.status
            raw = response.read(1024 * 1024)
    except HTTPError as exc:
        status_code = exc.code
        raw = exc.read(1024 * 1024)
    except Exception:
        return None, None, "request_failed"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return status_code, None, "invalid_json"
    return status_code, payload if isinstance(payload, dict) else None, None


def _known_token(value: JsonValue, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _bounded_duration(value: JsonValue) -> int | None:
    return value if type(value) is int and 0 <= value <= 86_400_000 else None


def _readiness_diagnostics(readiness: JsonObject) -> tuple[str, str, str, str, int | None, bool | None, str | None]:
    status = _known_token(readiness.get("status"), _READINESS_STATUSES, "unknown")
    error_code = _known_token(readiness.get("error_code"), _READINESS_ERROR_CODES, "UNKNOWN_PROVIDER_READINESS_ERROR")
    reason = _known_token(readiness.get("reason"), _READINESS_REASONS, "unknown")
    probe = _known_token(readiness.get("probe"), _READINESS_PROBES, "unknown")
    duration_ms = _bounded_duration(readiness.get("duration_ms"))
    retryable_value = readiness.get("retryable")
    retryable = retryable_value if type(retryable_value) is bool else None
    return status, error_code, reason, probe, duration_ms, retryable, _READINESS_ACTIONS.get(error_code)


def diagnose(*, api_base: str, wait_seconds: float, require_ready: bool) -> int:
    live_status, live, live_error = _get_json(f"{api_base.rstrip('/')}/health/live", timeout=3)
    if live_error or live_status is None or not 200 <= live_status < 300 or not live or live.get("status") != "ok":
        print(f"API: unhealthy status={live_status or 'unreachable'} error={live_error or 'invalid liveness response'}")
        print("根因: API liveness 不可达；当前不能归因于外部模型 provider，请检查 API 容器状态与 health log。")
        return 1
    print("API: healthy")

    deadline = time.monotonic() + max(0, wait_seconds)
    readiness: JsonObject = {}
    while True:
        _, payload, readiness_error = _get_json(f"{api_base.rstrip('/')}/health/ready", timeout=3)
        readiness = payload.get("model_provider", {}) if payload and isinstance(payload.get("model_provider"), dict) else {}
        if readiness_error or readiness.get("status") != "checking" or time.monotonic() >= deadline:
            if readiness_error:
                readiness = {
                    "status": "unknown",
                    "error_code": "READINESS_RESPONSE_UNAVAILABLE",
                    "reason": readiness_error,
                }
            break
        time.sleep(0.25)

    provider_status, error_code, reason, probe, duration_ms, retryable, action = _readiness_diagnostics(readiness)
    print(f"Model provider: {provider_status}")
    if error_code != "UNKNOWN_PROVIDER_READINESS_ERROR":
        print(f"error_code={error_code}")
    if reason != "unknown":
        print(f"reason={reason}")
    if probe != "unknown":
        print(f"probe={probe}")
    if duration_ms is not None:
        print(f"duration_ms={duration_ms}")
    if retryable is not None:
        print(f"retryable={str(retryable).lower()}")
    if action is not None:
        print(f"action={action}")
    if provider_status == "ready":
        print("结论: API 容器与外部模型 provider 均已就绪。")
    elif provider_status == "checking":
        print("结论: API 容器已存活；外部模型 provider 就绪探测仍在进行，不能把该探测当作镜像或容器启动失败。")
    else:
        print(
            f"根因: API 容器已存活；外部模型 provider 就绪探测失败（code={error_code}, reason={reason}）。这不是镜像启动失败，Compose dependency 报错只是次级症状。"
        )
    return 0 if provider_status == "ready" or not require_ready else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Print API liveness and cached model provider readiness.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.getenv("COMPOSE_ENV_FILE") or "docker/.env"),
    )
    parser.add_argument("--api-base")
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    host_port = os.getenv("HOST_PORT") or _env_value(args.env_file, "HOST_PORT") or "58080"
    api_base = args.api_base or os.getenv("API_BASE") or _env_value(args.env_file, "API_BASE") or f"http://localhost:{host_port}"
    return diagnose(
        api_base=api_base,
        wait_seconds=args.wait_seconds,
        require_ready=args.require_ready,
    )


if __name__ == "__main__":
    raise SystemExit(main())
