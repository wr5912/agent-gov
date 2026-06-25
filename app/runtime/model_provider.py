from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .agent_job_errors import (
    LITELLM_CLAUDE_CODE_COMPAT_FAILED,
    MODEL_AGENT_LOOP_CAPABILITY_FAILED,
    MODEL_PROVIDER_SIDECAR_UNAVAILABLE,
    MODEL_SCHEMA_EXACT_OUTPUT_FAILED,
    VLLM_CHAT_PROBE_FAILED,
    VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED,
    VLLM_MODELS_PROBE_FAILED,
    VLLM_TOOL_CALLING_UNSUPPORTED,
    ModelProviderCapabilityError,
    provider_api_key_configured,
)
from .json_types import JsonObject
from .model_provider_responses import (
    anthropic_content_has_tool_use,
    anthropic_tool_probe_body,
    first_choice_message,
    first_tool_call,
    message_content_text,
    parse_json_object,
)

logger = logging.getLogger(__name__)

ModelProviderBackend = Literal["vllm", "ollama", "openai_compatible", "anthropic_compatible"]
ProviderRouteName = Literal["direct_anthropic", "litellm_sidecar"]
ProbeStatus = Literal["skipped", "succeeded", "failed"]

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

    def route(self) -> ModelProviderRoute:
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
            return self._route

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
        return self.route().to_summary()

    def ensure_agent_runtime_ready(self) -> None:
        route = self.route()
        if route.backend == "anthropic_compatible" or self._agent_runtime_ready:
            return
        if route.requires_litellm_sidecar:
            self._ensure_sidecar_ready(route)
        if route.backend == "vllm":
            self._ensure_vllm_models_ready(route)
            self._ensure_vllm_chat_ready(route)
            tool_call = self._ensure_vllm_tool_calling_ready(route)
            self._ensure_model_agent_loop_ready(route, tool_call)
            self._ensure_schema_exact_output_ready(route)
        if route.requires_litellm_sidecar:
            self._ensure_anthropic_messages_compat_ready(
                route,
                base_url=route.sidecar_base_url,
                error_code=LITELLM_CLAUDE_CODE_COMPAT_FAILED,
                probe_label="claude",
                system_in_messages=False,
                retryable=True,
                action="verify LiteLLM Anthropic tool/streaming translation and upstream vLLM response shape",
            )
        elif route.backend == "vllm":
            self._ensure_anthropic_messages_compat_ready(
                route,
                base_url=route.claude_base_url,
                error_code=VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED,
                probe_label="vllm_direct",
                system_in_messages=True,
                retryable=False,
                action="vLLM /v1/messages must accept Claude Code requests (system in messages, tool_use, streaming); upgrade vLLM or unset MODEL_PROVIDER_VLLM_ALLOW_DIRECT to route via the LiteLLM sidecar",
            )
        self._agent_runtime_ready = True

    def _ensure_sidecar_ready(self, route: ModelProviderRoute) -> None:
        assert route.sidecar_base_url is not None
        result = self._http_json("GET", route.sidecar_base_url.rstrip("/") + "/health/readiness")
        if result.status_code is not None and 200 <= result.status_code < 300:
            return
        raise ModelProviderCapabilityError(
            error_code=MODEL_PROVIDER_SIDECAR_UNAVAILABLE,
            message="LiteLLM sidecar is not reachable or not ready.",
            route=route.route,
            probe="sidecar_readiness",
            endpoint=sanitize_endpoint(route.sidecar_base_url),
            status_code=result.status_code,
            duration_ms=result.duration_ms,
            retryable=True,
            action="start or restart agent-gov-litellm-sidecar and verify MODEL_PROVIDER_API_URL",
        )

    def _ensure_vllm_models_ready(self, route: ModelProviderRoute) -> None:
        endpoint = provider_v1_api_base(route.provider_endpoint)
        if endpoint is None:
            raise ModelProviderCapabilityError(
                error_code=VLLM_MODELS_PROBE_FAILED,
                message="vLLM model service URL is not configured.",
                route=route.route,
                probe="models",
                endpoint=None,
                retryable=True,
                action="configure MODEL_PROVIDER_API_URL with the running vLLM base URL",
            )
        result = self._http_json("GET", endpoint.rstrip("/") + "/models")
        if result.status_code is not None and 200 <= result.status_code < 300:
            return
        raise ModelProviderCapabilityError(
            error_code=VLLM_MODELS_PROBE_FAILED,
            message="vLLM model service /v1/models probe failed.",
            route=route.route,
            probe="models",
            endpoint=sanitize_endpoint(route.provider_endpoint),
            status_code=result.status_code,
            duration_ms=result.duration_ms,
            retryable=True,
            action="verify vLLM OpenAI-compatible server is running and MODEL_PROVIDER_API_URL has no /v1 suffix",
        )

    def _ensure_vllm_chat_ready(self, route: ModelProviderRoute) -> None:
        result = self._post_vllm_chat(
            route,
            {
                "model": self._agent_model(),
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 32,
                "temperature": 0,
            },
        )
        message = first_choice_message(result.body_json)
        if is_success(result) and message and message_content_text(message):
            return
        self._raise_capability_error(
            error_code=VLLM_CHAT_PROBE_FAILED,
            message="vLLM chat completion probe failed.",
            route=route,
            probe="chat",
            result=result,
            retryable=True,
            action="verify the vLLM OpenAI-compatible chat/completions endpoint and selected AGENT_MODEL",
        )

    def _ensure_vllm_tool_calling_ready(self, route: ModelProviderRoute) -> JsonObject:
        result = self._post_vllm_chat(
            route,
            {
                "model": self._agent_model(),
                "messages": [{"role": "user", "content": "Call the agent_gov_probe tool with value ok."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "agent_gov_probe",
                            "description": "Return a probe value.",
                            "parameters": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "agent_gov_probe"}},
                "max_tokens": 128,
                "temperature": 0,
            },
        )
        message = first_choice_message(result.body_json)
        tool_call = first_tool_call(message)
        if is_success(result) and tool_call:
            return tool_call
        self._raise_capability_error(
            error_code=VLLM_TOOL_CALLING_UNSUPPORTED,
            message="vLLM model service is reachable but tool calling probe failed.",
            route=route,
            probe="tool_calling",
            result=result,
            retryable=False,
            action="check vLLM tool parser settings or choose a tool-calling capable model",
        )

    def _ensure_model_agent_loop_ready(self, route: ModelProviderRoute, tool_call: JsonObject) -> None:
        tool_call_id = str(tool_call.get("id") or "call_1")
        function = tool_call.get("function")
        tool_name = function.get("name") if isinstance(function, dict) and isinstance(function.get("name"), str) else "agent_gov_probe"
        result = self._post_vllm_chat(
            route,
            {
                "model": self._agent_model(),
                "messages": [
                    {"role": "user", "content": "Call the agent_gov_probe tool with value ok, then answer DONE after the tool result."},
                    {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                    {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": '{"value":"ok"}'},
                ],
                "max_tokens": 64,
                "temperature": 0,
            },
        )
        message = first_choice_message(result.body_json)
        if is_success(result) and message_content_text(message):
            return
        self._raise_capability_error(
            error_code=MODEL_AGENT_LOOP_CAPABILITY_FAILED,
            message="Target model failed the two-step Claude Code-style tool loop probe.",
            route=route,
            probe="agent_tool_loop",
            result=result,
            retryable=False,
            action="choose a model that can continue after tool_result messages without looping or stalling",
        )

    def _ensure_schema_exact_output_ready(self, route: ModelProviderRoute) -> None:
        result = self._post_vllm_chat(
            route,
            {
                "model": self._agent_model(),
                "messages": [{"role": "user", "content": 'Return exactly this JSON object and nothing else: {"ok": true}'}],
                "response_format": {"type": "json_object"},
                "max_tokens": 64,
                "temperature": 0,
            },
        )
        message = first_choice_message(result.body_json)
        if is_success(result) and parse_json_object(message_content_text(message)) == {"ok": True}:
            return
        self._raise_capability_error(
            error_code=MODEL_SCHEMA_EXACT_OUTPUT_FAILED,
            message="Target model failed schema-exact JSON output probe.",
            route=route,
            probe="schema_exact_json",
            result=result,
            retryable=False,
            action="choose a model that can obey response_format=json_object and schema-exact governor outputs",
        )

    def _ensure_anthropic_messages_compat_ready(
        self,
        route: ModelProviderRoute,
        *,
        base_url: str | None,
        error_code: str,
        probe_label: str,
        system_in_messages: bool,
        retryable: bool,
        action: str,
    ) -> None:
        # Shared Claude Code Anthropic Messages compatibility probe for the LiteLLM sidecar
        # (sidecar_base_url) and vLLM direct (provider_endpoint). `system_in_messages` injects a
        # `system`-role entry inside `messages` (the exact shape Claude Code emits) so strict vLLM
        # Anthropic schemas that reject it fail-close instead of passing a false positive.
        base = normalize_url(base_url)
        if base is None:
            raise ModelProviderCapabilityError(
                error_code=error_code,
                message="Anthropic Messages compatibility route has no endpoint configured.",
                route=route.route,
                probe=f"{probe_label}_compat",
                endpoint=None,
                retryable=True,
                action=action,
            )
        messages_url = base + "/v1/messages"
        tool_result = self._http_json(
            "POST",
            messages_url,
            request_body=anthropic_tool_probe_body(self._agent_model(), system_in_messages=system_in_messages),
        )
        if not is_success(tool_result) or not anthropic_content_has_tool_use(tool_result.body_json):
            self._raise_capability_error(
                error_code=error_code,
                message="Anthropic Messages endpoint failed the Claude Code tool compatibility probe.",
                route=route,
                probe=f"{probe_label}_tool_compat",
                result=tool_result,
                endpoint=base_url,
                retryable=retryable,
                action=action,
            )
        self._ensure_anthropic_streaming_compat(
            route,
            messages_url=messages_url,
            endpoint=base_url,
            error_code=error_code,
            probe_label=probe_label,
            retryable=retryable,
            action=action,
        )

    def _ensure_anthropic_streaming_compat(
        self,
        route: ModelProviderRoute,
        *,
        messages_url: str,
        endpoint: str | None,
        error_code: str,
        probe_label: str,
        retryable: bool,
        action: str,
    ) -> None:
        stream_result = self._http_request(
            "POST",
            messages_url,
            request_body={
                "model": self._agent_model(),
                "max_tokens": 32,
                "stream": True,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            },
            accept="text/event-stream",
        )
        stream_text = stream_result.raw_body.decode("utf-8", errors="replace")
        if is_success(stream_result) and "event:" in stream_text and "event: error" not in stream_text:
            return
        self._raise_capability_error(
            error_code=error_code,
            message="Anthropic Messages endpoint failed the Claude Code streaming compatibility probe.",
            route=route,
            probe=f"{probe_label}_streaming",
            result=stream_result,
            endpoint=endpoint,
            retryable=retryable,
            action=action,
        )

    def _post_vllm_chat(self, route: ModelProviderRoute, request_body: Mapping[str, object]) -> HttpProbeResult:
        endpoint = provider_v1_api_base(route.provider_endpoint)
        if endpoint is None:
            return HttpProbeResult(status_code=None, duration_ms=0, reason="missing_provider_endpoint")
        return self._http_json("POST", endpoint.rstrip("/") + "/chat/completions", request_body=request_body)

    def _agent_model(self) -> str:
        value = getattr(self.settings, "agent_model", None)
        return value.strip() if isinstance(value, str) and value.strip() else "agent-gov-model"

    def _raise_capability_error(
        self,
        *,
        error_code: str,
        message: str,
        route: ModelProviderRoute,
        probe: str,
        result: HttpProbeResult,
        retryable: bool,
        action: str,
        endpoint: str | None = None,
    ) -> None:
        raise ModelProviderCapabilityError(
            error_code=error_code,
            message=message,
            route=route.route,
            probe=probe,
            endpoint=sanitize_endpoint(endpoint or route.provider_endpoint),
            status_code=result.status_code,
            duration_ms=result.duration_ms,
            retryable=retryable,
            action=action,
        )

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
        logger.warning(
            "event=%s provider_endpoint=%s reason=%s status_code=%s duration_ms=%s action=fallback_to_litellm_sidecar route_threshold=%s",
            VLLM_VERSION_PROBE_FAILED,
            endpoint,
            reason,
            result.status_code,
            result.duration_ms,
            getattr(self.settings, "model_provider_vllm_sidecar_threshold", "0.23.0"),
        )


def failed_version_probe(
    *,
    endpoint: str | None,
    reason: str,
    status_code: int | None = None,
    duration_ms: int | None = None,
    version: str | None = None,
) -> VersionProbeResult:
    return VersionProbeResult(
        status="failed",
        endpoint=endpoint,
        version=version,
        reason=reason,
        status_code=status_code,
        duration_ms=duration_ms,
        error_code=VLLM_VERSION_PROBE_FAILED,
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
