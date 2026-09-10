"""Runtime 的 OTLP 摄取配置；不依赖 AgentGov 查询或管理面客户端。"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class RuntimeOTelConfig:
    endpoint: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)


def _explicit_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.split(","):
        name, separator, value = item.strip().partition("=")
        name, value = unquote(name).strip().lower(), unquote(value).strip()
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name) or not value or any(ord(char) < 32 or ord(char) > 255 for char in value):
            raise ValueError("OTLP headers must contain valid name=value pairs")
        headers[name] = value
    return headers


def _trace_endpoint(environ: Mapping[str, str]) -> str:
    traces = environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    base = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    ingestion_base = environ.get("AGENTGOV_OTEL_BASE_URL", "").strip()
    endpoint = traces or (f"{base.rstrip('/')}/v1/traces" if base else "")
    if not endpoint and ingestion_base:
        endpoint = f"{ingestion_base.rstrip('/')}/api/public/otel/v1/traces"
    try:
        parsed = urlsplit(endpoint)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("LANGFUSE_ENABLED requires a valid OTLP HTTP endpoint without embedded credentials")
    return endpoint


def runtime_otel_config(environ: Mapping[str, str]) -> RuntimeOTelConfig | None:
    """关闭时忽略出口；开启时只允许显式 Header 或完整的摄取凭据。"""

    enabled = environ.get("LANGFUSE_ENABLED", "false").strip().lower()
    if enabled in {"false", "0", "no", "off", "f", "n"}:
        return None
    if enabled not in {"true", "1", "yes", "on", "t", "y"}:
        raise ValueError("LANGFUSE_ENABLED must be a boolean")
    endpoint = _trace_endpoint(environ)
    raw_headers = environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "").strip() or environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if raw_headers:
        headers = _explicit_headers(raw_headers)
    else:
        public_key = environ.get("AGENTGOV_OTEL_PUBLIC_KEY", "")
        secret_key = environ.get("AGENTGOV_OTEL_SECRET_KEY", "")
        if not public_key.strip() or not secret_key.strip():
            raise ValueError("LANGFUSE_ENABLED requires OTLP headers or both AGENTGOV_OTEL_PUBLIC_KEY and AGENTGOV_OTEL_SECRET_KEY")
        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        headers = {"authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"}
    return RuntimeOTelConfig(endpoint=endpoint, headers=headers)
