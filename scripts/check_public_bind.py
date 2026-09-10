#!/usr/bin/env python3
"""阻止单租户 AgentGov 在未显式确认时绑定到非 loopback 地址。"""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path


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
        bind_ip = os.environ.get(bind_key, values.get(bind_key, "127.0.0.1")).strip()
        allow = os.environ.get(allow_key, values.get(allow_key, "0")).strip()
        if allow not in {"0", "1"}:
            raise ValueError(f"{allow_key} must be 0 or 1")
        if not _is_loopback(bind_ip) and allow != "1":
            raise ValueError(
                f"public {label} bind requires {allow_key}=1; AgentGov is a single-tenant operator control plane without cross-user isolation",
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        require_public_bind_opt_in(_read_env(args.env_file))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
