from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .agent_job_errors import (
    MODEL_PROVIDER_CONFIGURATION_MISSING,
    MODEL_PROVIDER_NOT_CHECKED,
    MODEL_PROVIDER_PROBE_IN_PROGRESS,
    MODEL_PROVIDER_READINESS_PROBE_FAILED,
    VLLM_BASE_URL_INVALID,
    ModelProviderCapabilityError,
    provider_api_key_configured,
)
from .json_types import JsonObject

logger = logging.getLogger(__name__)

ModelProviderBackend = Literal["vllm", "ollama", "openai_compatible", "anthropic_compatible"]
ProviderRouteName = Literal["direct_anthropic", "litellm_sidecar"]
ProbeStatus = Literal["skipped", "succeeded", "failed"]
ProviderReadinessStatus = Literal["not_checked", "checking", "ready", "degraded"]

LITELLM_SIDECAR_BASE_URL = "http://agent-gov-litellm-sidecar:4000"
LOCAL_PROVIDER_DUMMY_API_KEY = "agent-gov-local-provider"
VLLM_VERSION_PROBE_FAILED = "VLLM_VERSION_PROBE_FAILED"
VLLM_VERSION_ROUTE = "VLLM_VERSION_ROUTE"

_WARNING_LAST_EMITTED_AT: dict[tuple[str, str], float] = {}
_ROUTE_INFO_LAST_EMITTED_AT: dict[tuple[str, str], float] = {}


@dataclass(frozen=True)
class HttpProbeResult:
    status_code: int | None
    duration_ms: int
    reason: str | None = None
    body_json: JsonObject | None = None
    raw_body: bytes = b""


class FormatterLMKwargs(TypedDict, total=False):
    api_key: str
    api_base: str


@dataclass(frozen=True)
class VersionProbeResult:
    status: ProbeStatus
    endpoint: str | None = None
    version: str | None = None
    reason: str | None = None
    status_code: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None

    def to_summary(self) -> JsonObject:
        summary: JsonObject = {
            "status": self.status,
            "endpoint": self.endpoint,
            "version": self.version,
            "reason": self.reason,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }
        return {key: value for key, value in summary.items() if value is not None}


@dataclass(frozen=True)
class ProviderReadinessSnapshot:
    status: ProviderReadinessStatus
    error_code: str | None = None
    message: str | None = None
    reason: str | None = None
    route: str | None = None
    probe: str | None = None
    status_code: int | None = None
    duration_ms: int | None = None
    retryable: bool | None = None
    action: str | None = None
    checked_at: str | None = None

    def to_summary(self) -> JsonObject:
        summary: JsonObject = {
            "status": self.status,
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "route": self.route,
            "probe": self.probe,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "retryable": self.retryable,
            "action": self.action,
            "checked_at": self.checked_at,
        }
        return {key: value for key, value in summary.items() if value is not None}


@dataclass(frozen=True)
class ModelProviderRoute:
    backend: ModelProviderBackend
    route: ProviderRouteName
    provider_endpoint: str | None
    claude_base_url: str | None
    formatter_api_base: str | None
    formatter_model_prefix: str | None
    version_probe: VersionProbeResult
    sidecar_base_url: str | None = None
    sidecar_required: bool = False
    provider_api_key_required: bool = True

    @property
    def requires_litellm_sidecar(self) -> bool:
        return self.route == "litellm_sidecar"

    def claude_env(self, api_key: str | None) -> dict[str, str]:
        env: dict[str, str] = {}
        key = api_key if provider_api_key_configured(api_key) else None
        if key:
            env["ANTHROPIC_API_KEY"] = key
        elif not self.provider_api_key_required:
            env["ANTHROPIC_API_KEY"] = LOCAL_PROVIDER_DUMMY_API_KEY
        if self.claude_base_url:
            env["ANTHROPIC_BASE_URL"] = self.claude_base_url
        return env

    def to_summary(self) -> JsonObject:
        return {
            "backend": self.backend,
            "route": self.route,
            "provider_endpoint_configured": bool(self.provider_endpoint),
            "provider_endpoint": sanitize_endpoint(self.provider_endpoint),
            "claude_base_url": sanitize_endpoint(self.claude_base_url),
            "formatter_api_base": sanitize_endpoint(self.formatter_api_base),
            "formatter_model_prefix": self.formatter_model_prefix,
            "sidecar_required": self.sidecar_required,
            "sidecar_base_url": sanitize_endpoint(self.sidecar_base_url),
            "provider_api_key_required": self.provider_api_key_required,
            "version_probe": self.version_probe.to_summary(),
        }


class ModelProviderRouter:
    """Select the runtime model route from explicit provider settings."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._route: ModelProviderRoute | None = None
        self._agent_runtime_ready = False
        self._probe_lock = threading.RLock()
        self._readiness_lock = threading.Lock()
        self._readiness = ProviderReadinessSnapshot(
            status="not_checked",
            error_code=MODEL_PROVIDER_NOT_CHECKED,
            message="Model provider readiness has not been checked yet; the API control plane is live.",
            retryable=True,
            action="wait for the background provider probe or trigger a model-backed action",
        )

    def route(self) -> ModelProviderRoute:
        with self._probe_lock:
            return self._resolve_route()

    def _resolve_route(self) -> ModelProviderRoute:
        if self._route is not None:
            return self._route

        backend = normalize_backend(getattr(self.settings, "model_provider_backend", "anthropic_compatible"))
        provider_endpoint = normalize_url(getattr(self.settings, "provider_api_url", None))
        if backend == "anthropic_compatible":
            self._route = ModelProviderRoute(
                backend=backend,
                route="direct_anthropic",
                provider_endpoint=provider_endpoint,
                claude_base_url=provider_endpoint,
                formatter_api_base=provider_endpoint,
                formatter_model_prefix="anthropic",
                version_probe=VersionProbeResult(status="skipped"),
                provider_api_key_required=True,
            )
            self._record_route_readiness(self._route)
            return self._route

        if backend == "vllm" and vllm_base_url_has_api_suffix(provider_endpoint):
            version_probe = failed_version_probe(
                endpoint=sanitize_endpoint(provider_endpoint),
                reason="vllm_base_url_must_not_end_in_v1",
                duration_ms=0,
                error_code=VLLM_BASE_URL_INVALID,
            )
        else:
            version_probe = self._probe_vllm_version(provider_endpoint) if backend == "vllm" else VersionProbeResult(status="skipped")
        if version_probe.status == "failed":
            self._warn_version_probe_failed(version_probe)

        # vLLM direct routing is an explicit opt-in exception (MODEL_PROVIDER_VLLM_ALLOW_DIRECT):
        # only when the probed version meets MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD do we point
        # Claude Code at vLLM's native Anthropic endpoint and skip the LiteLLM sidecar. Direct is
        # still gated by ensure_agent_runtime_ready() (it must pass a real Claude Code compat probe).
        # Below threshold, version unknown/unparseable, probe failed, or opt-in off -> sidecar.
        if backend == "vllm":
            allow_direct = bool(getattr(self.settings, "model_provider_vllm_allow_direct", False))
            meets_threshold = self._version_meets_threshold(version_probe)
            use_direct = allow_direct and meets_threshold
            if version_probe.status == "succeeded":
                self._log_version_route_decision(
                    version_probe,
                    allow_direct=allow_direct,
                    meets_threshold=meets_threshold,
                    chosen_route="direct_anthropic" if use_direct else "litellm_sidecar",
                )
            if use_direct:
                self._route = ModelProviderRoute(
                    backend=backend,
                    route="direct_anthropic",
                    provider_endpoint=provider_endpoint,
                    claude_base_url=provider_endpoint,
                    formatter_api_base=provider_v1_api_base(provider_endpoint),
                    formatter_model_prefix="openai",
                    version_probe=version_probe,
                    sidecar_required=False,
                    provider_api_key_required=False,
                )
                self._record_route_readiness(self._route)
                return self._route

        # Default: vLLM / OpenAI-compatible / Ollama are exposed to Claude Code through
        # the LiteLLM Anthropic-compatible sidecar. The upstream model service
        # URL remains MODEL_PROVIDER_API_URL; no second user URL is introduced.
        self._route = ModelProviderRoute(
            backend=backend,
            route="litellm_sidecar",
            provider_endpoint=provider_endpoint,
            claude_base_url=LITELLM_SIDECAR_BASE_URL,
            formatter_api_base=LITELLM_SIDECAR_BASE_URL,
            formatter_model_prefix="openai",
            version_probe=version_probe,
            sidecar_base_url=LITELLM_SIDECAR_BASE_URL,
            sidecar_required=True,
            provider_api_key_required=False,
        )
        self._record_route_readiness(self._route)
        return self._route

    def _version_meets_threshold(self, version_probe: VersionProbeResult) -> bool:
        if version_probe.status != "succeeded" or not version_probe.version:
            return False
        detected = parse_version(version_probe.version)
        threshold = parse_version(str(getattr(self.settings, "model_provider_vllm_sidecar_threshold", "0.23.0")))
        if detected is None or threshold is None:
            return False
        return detected >= threshold

    def _log_version_route_decision(
        self,
        version_probe: VersionProbeResult,
        *,
        allow_direct: bool,
        meets_threshold: bool,
        chosen_route: str,
    ) -> None:
        # §6.2: a successful version probe is an expected routing decision -> info, not warning.
        # Throttled per (endpoint, route) so /health probing does not spam logs.
        endpoint = version_probe.endpoint or "unconfigured"
        key = (endpoint, chosen_route)
        ttl_seconds = int(getattr(self.settings, "model_provider_warning_ttl_seconds", 300))
        now = time.monotonic()
        last = _ROUTE_INFO_LAST_EMITTED_AT.get(key)
        if last is not None and now - last < ttl_seconds:
            return
        _ROUTE_INFO_LAST_EMITTED_AT[key] = now
        logger.info(
            "event=%s provider_endpoint=%s version=%s threshold=%s meets_threshold=%s allow_direct=%s route=%s",
            VLLM_VERSION_ROUTE,
            endpoint,
            version_probe.version,
            getattr(self.settings, "model_provider_vllm_sidecar_threshold", "0.23.0"),
            meets_threshold,
            allow_direct,
            chosen_route,
        )

    def claude_env(self) -> dict[str, str]:
        return self.route().claude_env(getattr(self.settings, "provider_api_key", None))

    def formatter_model_name(self, model: str) -> str:
        if "/" in model:
            return model
        prefix = self.route().formatter_model_prefix
        return f"{prefix}/{model}" if prefix else model

    def formatter_kwargs(self) -> FormatterLMKwargs:
        route = self.route()
        kwargs: FormatterLMKwargs = {}
        api_key = getattr(self.settings, "provider_api_key", None)
        if provider_api_key_configured(api_key):
            kwargs["api_key"] = api_key
        elif not route.provider_api_key_required:
            kwargs["api_key"] = LOCAL_PROVIDER_DUMMY_API_KEY
        if route.formatter_api_base:
            kwargs["api_base"] = route.formatter_api_base
        return kwargs

    def provider_credentials_configured(self) -> bool:
        route = self.route()
        if provider_api_key_configured(getattr(self.settings, "provider_api_key", None)):
            return True
        return not route.provider_api_key_required and bool(route.provider_endpoint)

    def health_summary(self) -> JsonObject:
        route = self._route
        if route is not None:
            summary = route.to_summary()
        else:
            backend = normalize_backend(getattr(self.settings, "model_provider_backend", "anthropic_compatible"))
            provider_endpoint = normalize_url(getattr(self.settings, "provider_api_url", None))
            summary = {
                "backend": backend,
                "route": "direct_anthropic" if backend == "anthropic_compatible" else None,
                "provider_endpoint_configured": bool(provider_endpoint),
                "provider_endpoint": sanitize_endpoint(provider_endpoint),
                "claude_base_url": None,
                "formatter_api_base": None,
                "formatter_model_prefix": None,
                "sidecar_required": None,
                "sidecar_base_url": None,
                "provider_api_key_required": backend == "anthropic_compatible",
                "version_probe": None,
            }
        summary["readiness"] = self.readiness_summary()
        return summary

    def readiness_summary(self) -> JsonObject:
        with self._readiness_lock:
            return self._readiness.to_summary()

    def mark_readiness_checking(self) -> None:
        self._set_readiness(
            ProviderReadinessSnapshot(
                status="checking",
                error_code=MODEL_PROVIDER_PROBE_IN_PROGRESS,
                message="Model provider readiness probe is running in the background; API liveness is unaffected.",
                retryable=True,
                action="wait for /health/ready to report ready or a specific degraded error",
            )
        )

    def refresh_readiness(self) -> JsonObject:
        """Refresh provider routing in one background-safe, single-flight probe."""
        self.mark_readiness_checking()
        try:
            with self._probe_lock:
                self._route = None
                self._agent_runtime_ready = False
                self._resolve_route()
        except Exception as exc:
            self._set_readiness(
                ProviderReadinessSnapshot(
                    status="degraded",
                    error_code=MODEL_PROVIDER_READINESS_PROBE_FAILED,
                    message="Unexpected model provider readiness probe failure; the API control plane remains live.",
                    reason=exc.__class__.__name__,
                    probe="provider_route",
                    retryable=True,
                    action="inspect API logs and verify model provider settings before retrying",
                    checked_at=_utc_now(),
                )
            )
        return self.readiness_summary()

    def warm_agent_runtime_readiness(self) -> JsonObject:
        """启动期预热完整能力门；与首个请求共享同一把锁和同一份成功缓存。"""
        self.mark_readiness_checking()
        try:
            self.ensure_agent_runtime_ready()
        except ModelProviderCapabilityError:
            # ensure_agent_runtime_ready 已把精确错误写入 readiness；API 控制面继续启动。
            pass
        except Exception as exc:
            self._set_readiness(
                ProviderReadinessSnapshot(
                    status="degraded",
                    error_code=MODEL_PROVIDER_READINESS_PROBE_FAILED,
                    message="Unexpected model provider capability warmup failure; the API control plane remains live.",
                    reason=exc.__class__.__name__,
                    probe="agent_runtime_capabilities",
                    retryable=True,
                    action="inspect API logs and verify model provider settings before retrying",
                    checked_at=_utc_now(),
                )
            )
        return self.readiness_summary()

    def ensure_agent_runtime_ready(self) -> None:
        try:
            with self._probe_lock:
                self._ensure_agent_runtime_ready()
        except ModelProviderCapabilityError as exc:
            self._record_capability_failure(exc)
            raise
        self._set_readiness(
            ProviderReadinessSnapshot(
                status="ready",
                message="Model provider passed the Agent runtime capability checks.",
                route=self._route.route if self._route else None,
                probe="agent_runtime_capabilities",
                retryable=False,
                checked_at=_utc_now(),
            )
        )

    def _ensure_agent_runtime_ready(self) -> None:
        route = self.route()
        if route.backend == "anthropic_compatible" or self._agent_runtime_ready:
            return
        from .model_provider_capabilities import ensure_model_provider_capabilities

        ensure_model_provider_capabilities(self, route)
        self._agent_runtime_ready = True

    def _record_route_readiness(self, route: ModelProviderRoute) -> None:
        probe = route.version_probe
        if probe.status == "failed":
            invalid_base_url = probe.error_code == VLLM_BASE_URL_INVALID
            self._set_readiness(
                ProviderReadinessSnapshot(
                    status="degraded",
                    error_code=probe.error_code or VLLM_VERSION_PROBE_FAILED,
                    message=(
                        "MODEL_PROVIDER_API_URL must be the vLLM service base URL without a trailing /v1."
                        if invalid_base_url
                        else "vLLM version probe failed; the API control plane remains live while model routing is degraded."
                    ),
                    reason=probe.reason,
                    route=route.route,
                    probe="vllm_version",
                    status_code=probe.status_code,
                    duration_ms=probe.duration_ms,
                    retryable=not invalid_base_url,
                    action=(
                        "remove the trailing /v1 from MODEL_PROVIDER_API_URL"
                        if invalid_base_url
                        else "verify external vLLM is reachable and MODEL_PROVIDER_API_URL points to its base URL"
                    ),
                    checked_at=_utc_now(),
                )
            )
            return

        provider_key = getattr(self.settings, "provider_api_key", None)
        credentials_ready = provider_api_key_configured(provider_key) or (not route.provider_api_key_required and bool(route.provider_endpoint))
        if not credentials_ready:
            self._set_readiness(
                ProviderReadinessSnapshot(
                    status="degraded",
                    error_code=MODEL_PROVIDER_CONFIGURATION_MISSING,
                    message="Model provider credentials or endpoint are not configured; the API control plane remains live.",
                    reason="missing_provider_configuration",
                    route=route.route,
                    probe="configuration",
                    retryable=True,
                    action="configure MODEL_PROVIDER_API_KEY and MODEL_PROVIDER_API_URL for the selected backend",
                    checked_at=_utc_now(),
                )
            )
            return

        self._set_readiness(
            ProviderReadinessSnapshot(
                status="ready",
                message=(
                    "vLLM version probe succeeded and model routing is configured."
                    if probe.status == "succeeded"
                    else "Model provider routing configuration is ready."
                ),
                route=route.route,
                probe="vllm_version" if probe.status == "succeeded" else "configuration",
                status_code=probe.status_code,
                duration_ms=probe.duration_ms,
                retryable=False,
                checked_at=_utc_now(),
            )
        )

    def _record_capability_failure(self, exc: ModelProviderCapabilityError) -> None:
        details = exc.raw_output_json or {}
        self._set_readiness(
            ProviderReadinessSnapshot(
                status="degraded",
                error_code=exc.error_code,
                message=str(details.get("message") or exc),
                reason=str(details.get("reason")) if details.get("reason") else None,
                route=str(details.get("route")) if details.get("route") else None,
                probe=str(details.get("probe")) if details.get("probe") else None,
                status_code=details.get("status_code") if isinstance(details.get("status_code"), int) else None,
                duration_ms=details.get("duration_ms") if isinstance(details.get("duration_ms"), int) else None,
                retryable=bool(details.get("retryable")),
                action=str(details.get("action")) if details.get("action") else None,
                checked_at=_utc_now(),
            )
        )

    def _set_readiness(self, snapshot: ProviderReadinessSnapshot) -> None:
        with self._readiness_lock:
            self._readiness = snapshot

    def _http_json(self, method: str, endpoint: str, request_body: Mapping[str, object] | None = None) -> HttpProbeResult:
        result = self._http_request(method, endpoint, request_body=request_body)
        parsed: JsonObject | None = None
        if result.raw_body:
            try:
                loaded = json.loads(result.raw_body.decode("utf-8"))
                if isinstance(loaded, dict):
                    parsed = loaded
            except Exception:
                parsed = None
        return HttpProbeResult(status_code=result.status_code, duration_ms=result.duration_ms, reason=result.reason, body_json=parsed, raw_body=result.raw_body)

    def _http_request(
        self,
        method: str,
        endpoint: str,
        request_body: Mapping[str, object] | None = None,
        *,
        accept: str = "application/json",
    ) -> HttpProbeResult:
        start = time.monotonic()
        timeout = float(getattr(self.settings, "model_provider_probe_timeout_seconds", 3))
        headers = {"Accept": accept}
        data = None
        if request_body is not None:
            data = json.dumps(request_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        api_key = getattr(self.settings, "provider_api_key", None)
        if provider_api_key_configured(api_key):
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            with urlopen(Request(endpoint, data=data, headers=headers, method=method), timeout=timeout) as response:
                status_code = getattr(response, "status", None) or response.getcode()
                raw = response.read(1024 * 1024)
        except HTTPError as exc:
            return HttpProbeResult(status_code=exc.code, duration_ms=elapsed_ms(start), reason=f"http_{exc.code}")
        except TimeoutError:
            return HttpProbeResult(status_code=None, duration_ms=elapsed_ms(start), reason="timeout")
        except URLError as exc:
            reason = "timeout" if isinstance(exc.reason, TimeoutError) else "connection_error"
            return HttpProbeResult(status_code=None, duration_ms=elapsed_ms(start), reason=reason)
        except Exception as exc:
            return HttpProbeResult(status_code=None, duration_ms=elapsed_ms(start), reason=exc.__class__.__name__)
        return HttpProbeResult(status_code=status_code, duration_ms=elapsed_ms(start), reason=None, raw_body=raw)

    def _probe_vllm_version(self, provider_endpoint: str | None) -> VersionProbeResult:
        endpoint = version_probe_url(provider_endpoint)
        sanitized = sanitize_endpoint(provider_endpoint)
        if endpoint is None:
            return failed_version_probe(endpoint=sanitized, reason="missing_provider_endpoint")
        result = self._http_request("GET", endpoint)
        if result.reason:
            return failed_version_probe(endpoint=sanitized, reason=result.reason, status_code=result.status_code, duration_ms=result.duration_ms)
        if result.status_code is None or result.status_code < 200 or result.status_code >= 300:
            return failed_version_probe(
                endpoint=sanitized,
                reason=f"http_{result.status_code}" if result.status_code is not None else "http_status",
                status_code=result.status_code,
                duration_ms=result.duration_ms,
            )
        return version_probe_result_from_body(sanitized, result)

    def _warn_version_probe_failed(self, result: VersionProbeResult) -> None:
        endpoint = result.endpoint or "unconfigured"
        reason = result.reason or "unknown"
        ttl_seconds = int(getattr(self.settings, "model_provider_warning_ttl_seconds", 300))
        key = (endpoint, reason)
        now = time.monotonic()
        last = _WARNING_LAST_EMITTED_AT.get(key)
        if last is not None and now - last < ttl_seconds:
            return
        _WARNING_LAST_EMITTED_AT[key] = now
        action = "reject_invalid_vllm_base_url" if result.error_code == VLLM_BASE_URL_INVALID else "fallback_to_litellm_sidecar"
        logger.warning(
            "event=%s provider_endpoint=%s reason=%s status_code=%s duration_ms=%s action=%s route_threshold=%s",
            result.error_code or VLLM_VERSION_PROBE_FAILED,
            endpoint,
            reason,
            result.status_code,
            result.duration_ms,
            action,
            getattr(self.settings, "model_provider_vllm_sidecar_threshold", "0.23.0"),
        )


def failed_version_probe(
    *,
    endpoint: str | None,
    reason: str,
    status_code: int | None = None,
    duration_ms: int | None = None,
    version: str | None = None,
    error_code: str = VLLM_VERSION_PROBE_FAILED,
) -> VersionProbeResult:
    return VersionProbeResult(
        status="failed",
        endpoint=endpoint,
        version=version,
        reason=reason,
        status_code=status_code,
        duration_ms=duration_ms,
        error_code=error_code,
    )


def version_probe_result_from_body(endpoint: str | None, result: HttpProbeResult) -> VersionProbeResult:
    try:
        body = json.loads(result.raw_body.decode("utf-8"))
    except Exception:
        return failed_version_probe(endpoint=endpoint, reason="invalid_json", status_code=result.status_code, duration_ms=result.duration_ms)
    if not isinstance(body, Mapping):
        return failed_version_probe(endpoint=endpoint, reason="invalid_json", status_code=result.status_code, duration_ms=result.duration_ms)
    version = body.get("version")
    if not isinstance(version, str) or not version.strip():
        return failed_version_probe(endpoint=endpoint, reason="missing_version", status_code=result.status_code, duration_ms=result.duration_ms)
    if parse_version(version) is None:
        return failed_version_probe(
            endpoint=endpoint,
            version=version,
            reason="invalid_version",
            status_code=result.status_code,
            duration_ms=result.duration_ms,
        )
    return VersionProbeResult(status="succeeded", endpoint=endpoint, version=version.strip(), status_code=result.status_code, duration_ms=result.duration_ms)


def normalize_backend(value: object) -> ModelProviderBackend:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"vllm", "ollama", "openai_compatible", "anthropic_compatible"}:
            return normalized  # type: ignore[return-value]
    raise ValueError("MODEL_PROVIDER_BACKEND must be one of: vllm, ollama, openai_compatible, anthropic_compatible")


def normalize_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped.rstrip("/") if stripped else None


def version_probe_url(provider_endpoint: str | None) -> str | None:
    endpoint = normalize_url(provider_endpoint)
    if not endpoint:
        return None
    return endpoint + "/version"


def vllm_base_url_has_api_suffix(provider_endpoint: str | None) -> bool:
    endpoint = normalize_url(provider_endpoint)
    if not endpoint:
        return False
    return urlsplit(endpoint).path.rstrip("/").endswith("/v1")


def provider_v1_api_base(provider_endpoint: str | None) -> str | None:
    endpoint = normalize_url(provider_endpoint)
    if not endpoint:
        return None
    if endpoint.endswith("/v1"):
        return endpoint
    return endpoint + "/v1"


def sanitize_endpoint(value: str | None) -> str | None:
    endpoint = normalize_url(value)
    if not endpoint:
        return None
    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, "", "", ""))


def elapsed_ms(start: float) -> int:
    return max(0, round((time.monotonic() - start) * 1000))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_version(value: str) -> tuple[int, ...] | None:
    head = value.strip().split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for item in head.split("."):
        if not item.isdigit():
            return None
        parts.append(int(item))
    return tuple(parts) if parts else None


def is_success(result: HttpProbeResult) -> bool:
    return result.status_code is not None and 200 <= result.status_code < 300
