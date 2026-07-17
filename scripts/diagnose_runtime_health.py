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
    except Exception as exc:
        return None, None, f"{exc.__class__.__name__}: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return status_code, None, "response is not valid JSON"
    return status_code, payload if isinstance(payload, dict) else None, None


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

    provider_status = str(readiness.get("status") or "unknown")
    print(f"Model provider: {provider_status}")
    for key in ("error_code", "reason", "probe", "duration_ms", "retryable", "action", "checked_at"):
        value = readiness.get(key)
        if value is not None:
            print(f"{key}={value}")
    if provider_status == "ready":
        print("结论: API 容器与外部模型 provider 均已就绪。")
    elif provider_status == "checking":
        print("结论: API 容器已存活；外部模型 provider 就绪探测仍在进行，不能把该探测当作镜像或容器启动失败。")
    else:
        code = str(readiness.get("error_code") or "UNKNOWN_PROVIDER_READINESS_ERROR")
        reason = str(readiness.get("reason") or "unknown")
        print(
            f"根因: API 容器已存活；外部模型 provider 就绪探测失败（code={code}, reason={reason}）。这不是镜像启动失败，Compose dependency 报错只是次级症状。"
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
