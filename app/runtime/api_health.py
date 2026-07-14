from __future__ import annotations

import json
import os
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


class ApiHealthSettings(Protocol):
    api_port: int


def internal_api_health_url(settings: ApiHealthSettings) -> str:
    marker = os.getenv("RUNTIME_CONTAINER", "").strip().lower()
    host = "claude-agent-api" if marker in {"1", "true", "yes", "on", "container"} else "127.0.0.1"
    return f"http://{host}:{settings.api_port}/health"


def api_health_ready(url: str) -> bool:
    """Require a successful HTTP status and the AgentGov health payload."""

    try:
        with urlopen(url, timeout=3) as response:  # noqa: S310 - fixed internal runtime URL.
            if response.status < 200 or response.status >= 300:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and payload.get("status") == "ok"
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return False
