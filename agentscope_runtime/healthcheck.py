"""Signed in-container liveness probe for the private Runtime surface."""

from __future__ import annotations

import os
import subprocess
import urllib.request

from .settings import RUNTIME_USER_ID
from .signing import runtime_gateway_headers


def main() -> None:
    subprocess.run(  # noqa: S603 - 固定 argv，只探测镜像内 bwrap
        [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--share-net",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--",
            "/usr/bin/true",
        ],
        check=True,
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    secret = os.environ["AGENTGOV_RUNTIME_SHARED_SECRET"]
    host = os.environ.get("AGENTSCOPE_RUNTIME_HOST", "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = os.environ.get("AGENTSCOPE_RUNTIME_PORT", "8090")
    headers = {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(secret, RUNTIME_USER_ID, "GET", "/health"),
    }
    request = urllib.request.Request(f"http://{host}:{port}/health", headers=headers)
    with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310 - fixed in-container URL
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
