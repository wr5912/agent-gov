#!/usr/bin/env python3
"""阻止单租户 AgentGov 在未显式确认时绑定到非 loopback 地址。"""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path

PROJECT_HOST_PORT_MIN = 50400
PROJECT_HOST_PORT_MAX = 50499
PROJECT_HOST_PORTS = (
    ("HOST_PORT", 50400),
    ("FRONTEND_HOST_PORT", 50401),
    ("LANGFUSE_HOST_PORT", 50402),
    ("LANGFUSE_MINIO_HOST_PORT", 50403),
    ("LANGFUSE_MINIO_CONSOLE_HOST_PORT", 50404),
)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _is_loopback(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def require_public_bind_opt_in(values: dict[str, str]) -> None:
    checks = (
        ("AgentGov API", "API_BIND_IP", "API_ALLOW_PUBLIC_BIND"),
        ("AgentGov UI", "FRONTEND_BIND_IP", "FRONTEND_ALLOW_PUBLIC_BIND"),
        ("Langfuse", "LANGFUSE_BIND_IP", "LANGFUSE_ALLOW_PUBLIC_BIND"),
    )
    for label, bind_key, allow_key in checks:
        bind_ip = values.get(bind_key, "127.0.0.1").strip()
        allow = values.get(allow_key, "0").strip()
        if allow not in {"0", "1"}:
            raise ValueError(f"{allow_key} must be 0 or 1")
        if not _is_loopback(bind_ip) and allow != "1":
            raise ValueError(
                f"public {label} bind requires {allow_key}=1; AgentGov is a single-tenant operator control plane without cross-user isolation",
            )


def require_project_host_ports(values: dict[str, str]) -> None:
    resolved: dict[str, int] = {}
    for key, default in PROJECT_HOST_PORTS:
        raw = values.get(key, str(default)).strip()
        if not raw.isdecimal():
            raise ValueError(f"{key} must be an integer in {PROJECT_HOST_PORT_MIN}-{PROJECT_HOST_PORT_MAX}")
        port = int(raw)
        if not PROJECT_HOST_PORT_MIN <= port <= PROJECT_HOST_PORT_MAX:
            raise ValueError(f"{key} must be in {PROJECT_HOST_PORT_MIN}-{PROJECT_HOST_PORT_MAX}")
        resolved[key] = port
    duplicates = sorted(port for port in set(resolved.values()) if list(resolved.values()).count(port) > 1)
    if duplicates:
        raise ValueError(f"project host ports must be unique; duplicated: {duplicates}")


def _configured_value(values: dict[str, str], key: str) -> str:
    return values.get(key, "").strip()


def _image_version(image: str) -> str | None:
    """Return the explicit image tag without confusing a registry port for it."""

    without_digest = image.split("@", 1)[0]
    final_component = without_digest.rsplit("/", 1)[-1]
    if ":" not in final_component:
        return None
    _, version = final_component.rsplit(":", 1)
    return version or None


def require_paired_langfuse_images(values: dict[str, str]) -> None:
    """禁止只覆盖 Langfuse web/worker 之一或配置不同版本。"""

    web = _configured_value(values, "LANGFUSE_WEB_IMAGE")
    worker = _configured_value(values, "LANGFUSE_WORKER_IMAGE")
    if bool(web) != bool(worker):
        raise ValueError("LANGFUSE_WEB_IMAGE and LANGFUSE_WORKER_IMAGE must be overridden together")
    if not web:
        return
    web_version = _image_version(web)
    worker_version = _image_version(worker)
    if web_version is None or worker_version is None or web_version != worker_version:
        raise ValueError("LANGFUSE_WEB_IMAGE and LANGFUSE_WORKER_IMAGE must use the same explicit version tag")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        values = _read_env(args.env_file)
        require_public_bind_opt_in(values)
        require_project_host_ports(values)
        require_paired_langfuse_images(values)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
