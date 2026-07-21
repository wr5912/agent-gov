from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .agent_job_errors import (
    LITELLM_CLAUDE_CODE_COMPAT_FAILED,
    MODEL_AGENT_LOOP_CAPABILITY_FAILED,
    MODEL_PROVIDER_SIDECAR_UNAVAILABLE,
    MODEL_SCHEMA_EXACT_OUTPUT_FAILED,
    VLLM_BASE_URL_INVALID,
    VLLM_CHAT_PROBE_FAILED,
    VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED,
    VLLM_MODELS_PROBE_FAILED,
    VLLM_TOOL_CALLING_UNSUPPORTED,
    ModelProviderCapabilityError,
)
from .json_types import JsonObject
from .model_provider import (
    VLLM_VERSION_PROBE_FAILED,
    HttpProbeResult,
    ModelProviderRoute,
    is_success,
    normalize_url,
    provider_v1_api_base,
    sanitize_endpoint,
)
from .model_provider_responses import (
    anthropic_content_has_tool_use,
    anthropic_tool_probe_body,
    first_choice_message,
    first_tool_call,
    message_content_text,
    parse_json_object,
)


class ModelProviderProbeHost(Protocol):
    settings: object

    def _http_json(
        self,
        method: str,
        endpoint: str,
        request_body: Mapping[str, object] | None = None,
    ) -> HttpProbeResult: ...

    def _http_request(
        self,
        method: str,
        endpoint: str,
        request_body: Mapping[str, object] | None = None,
        *,
        accept: str = "application/json",
    ) -> HttpProbeResult: ...


def ensure_model_provider_capabilities(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
) -> None:
    _raise_if_vllm_route_unreachable(route)
    if route.requires_litellm_sidecar:
        _ensure_sidecar_ready(host, route)
    if route.backend == "vllm":
        _ensure_vllm_models_ready(host, route)
        _ensure_vllm_chat_ready(host, route)
        tool_call = _ensure_vllm_tool_calling_ready(host, route)
        _ensure_model_agent_loop_ready(host, route, tool_call)
        _ensure_schema_exact_output_ready(host, route)
    if route.requires_litellm_sidecar:
        _ensure_anthropic_messages_compat_ready(
            host,
            route,
            base_url=route.sidecar_base_url,
            error_code=LITELLM_CLAUDE_CODE_COMPAT_FAILED,
            probe_label="claude",
            system_in_messages=False,
            retryable=True,
            action="verify LiteLLM Anthropic tool/streaming translation and upstream vLLM response shape",
        )
    elif route.backend == "vllm":
        _ensure_anthropic_messages_compat_ready(
            host,
            route,
            base_url=route.claude_base_url,
            error_code=VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED,
            probe_label="vllm_direct",
            system_in_messages=True,
            retryable=False,
            action=(
                "vLLM /v1/messages must accept Claude Code requests (system in messages, tool_use, streaming); "
                "upgrade vLLM or unset MODEL_PROVIDER_VLLM_ALLOW_DIRECT to route via the LiteLLM sidecar"
            ),
        )


def _raise_if_vllm_route_unreachable(route: ModelProviderRoute) -> None:
    if route.backend != "vllm" or route.version_probe.status != "failed":
        return
    probe = route.version_probe
    reason = probe.reason or "unknown"
    if probe.error_code == VLLM_BASE_URL_INVALID:
        raise ModelProviderCapabilityError(
            error_code=VLLM_BASE_URL_INVALID,
            message="MODEL_PROVIDER_API_URL is not a valid vLLM service base URL.",
            route=route.route,
            probe="vllm_version",
            endpoint=probe.endpoint,
            reason=reason,
            status_code=probe.status_code,
            duration_ms=probe.duration_ms,
            retryable=False,
            action="remove the trailing /v1 from MODEL_PROVIDER_API_URL, then restart the API",
        )
    transport_failure = reason in {
        "timeout",
        "connection_error",
        "missing_provider_endpoint",
        "http_408",
        "http_429",
    }
    if not transport_failure and not reason.startswith("http_5"):
        return
    message = (
        "External vLLM readiness probe timed out; the Agent request was not started."
        if reason == "timeout"
        else "External vLLM is unreachable or not ready; the Agent request was not started."
    )
    raise ModelProviderCapabilityError(
        error_code=probe.error_code or VLLM_VERSION_PROBE_FAILED,
        message=message,
        route=route.route,
        probe="vllm_version",
        endpoint=probe.endpoint,
        reason=reason,
        status_code=probe.status_code,
        duration_ms=probe.duration_ms,
        retryable=True,
        action=("verify the external vLLM process and MODEL_PROVIDER_API_URL, then retry; the AgentGov API remains live at /health/live"),
    )


def _ensure_sidecar_ready(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
) -> None:
    assert route.sidecar_base_url is not None
    result = host._http_json("GET", route.sidecar_base_url.rstrip("/") + "/health/readiness")
    if is_success(result):
        return
    raise ModelProviderCapabilityError(
        error_code=MODEL_PROVIDER_SIDECAR_UNAVAILABLE,
        message="LiteLLM sidecar is not reachable or not ready.",
        route=route.route,
        probe="sidecar_readiness",
        endpoint=sanitize_endpoint(route.sidecar_base_url),
        reason=result.reason,
        status_code=result.status_code,
        duration_ms=result.duration_ms,
        retryable=True,
        action="start or restart agent-gov-litellm-sidecar and verify MODEL_PROVIDER_API_URL",
    )


def _ensure_vllm_models_ready(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
) -> None:
    endpoint = provider_v1_api_base(route.provider_endpoint)
    if endpoint is None:
        raise ModelProviderCapabilityError(
            error_code=VLLM_MODELS_PROBE_FAILED,
            message="vLLM model service URL is not configured.",
            route=route.route,
            probe="models",
            endpoint=None,
            reason="missing_provider_endpoint",
            retryable=True,
            action="configure MODEL_PROVIDER_API_URL with the running vLLM base URL",
        )
    result = host._http_json("GET", endpoint.rstrip("/") + "/models")
    if is_success(result):
        return
    raise ModelProviderCapabilityError(
        error_code=VLLM_MODELS_PROBE_FAILED,
        message="vLLM model service /v1/models probe failed.",
        route=route.route,
        probe="models",
        endpoint=sanitize_endpoint(route.provider_endpoint),
        reason=result.reason,
        status_code=result.status_code,
        duration_ms=result.duration_ms,
        retryable=True,
        action="verify vLLM OpenAI-compatible server is running and MODEL_PROVIDER_API_URL has no /v1 suffix",
    )


def _ensure_vllm_chat_ready(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
) -> None:
    result = _post_vllm_chat(
        host,
        route,
        {
            "model": _agent_model(host),
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 32,
            "temperature": 0,
        },
    )
    message = first_choice_message(result.body_json)
    if is_success(result) and message and message_content_text(message):
        return
    _raise_capability_error(
        error_code=VLLM_CHAT_PROBE_FAILED,
        message="vLLM chat completion probe failed.",
        route=route,
        probe="chat",
        result=result,
        retryable=True,
        action="verify the vLLM OpenAI-compatible chat/completions endpoint and selected AGENT_MODEL",
    )


def _ensure_vllm_tool_calling_ready(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
) -> JsonObject:
    result = _post_vllm_chat(
        host,
        route,
        {
            "model": _agent_model(host),
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
    _raise_capability_error(
        error_code=VLLM_TOOL_CALLING_UNSUPPORTED,
        message="vLLM model service is reachable but tool calling probe failed.",
        route=route,
        probe="tool_calling",
        result=result,
        retryable=False,
        action="check vLLM tool parser settings or choose a tool-calling capable model",
    )


def _ensure_model_agent_loop_ready(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
    tool_call: JsonObject,
) -> None:
    tool_call_id = str(tool_call.get("id") or "call_1")
    function = tool_call.get("function")
    tool_name = function.get("name") if isinstance(function, dict) and isinstance(function.get("name"), str) else "agent_gov_probe"
    result = _post_vllm_chat(
        host,
        route,
        {
            "model": _agent_model(host),
            "messages": [
                {
                    "role": "user",
                    "content": "Call the agent_gov_probe tool with value ok, then answer DONE after the tool result.",
                },
                {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": '{"value":"ok"}',
                },
            ],
            "max_tokens": 64,
            "temperature": 0,
        },
    )
    message = first_choice_message(result.body_json)
    if is_success(result) and message_content_text(message):
        return
    _raise_capability_error(
        error_code=MODEL_AGENT_LOOP_CAPABILITY_FAILED,
        message="Target model failed the two-step Claude Code-style tool loop probe.",
        route=route,
        probe="agent_tool_loop",
        result=result,
        retryable=False,
        action="choose a model that can continue after tool_result messages without looping or stalling",
    )


def _ensure_schema_exact_output_ready(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
) -> None:
    result = _post_vllm_chat(
        host,
        route,
        {
            "model": _agent_model(host),
            "messages": [
                {
                    "role": "user",
                    "content": 'Return exactly this JSON object and nothing else: {"ok": true}',
                }
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 64,
            "temperature": 0,
        },
    )
    message = first_choice_message(result.body_json)
    if is_success(result) and parse_json_object(message_content_text(message)) == {"ok": True}:
        return
    _raise_capability_error(
        error_code=MODEL_SCHEMA_EXACT_OUTPUT_FAILED,
        message="Target model failed schema-exact JSON output probe.",
        route=route,
        probe="schema_exact_json",
        result=result,
        retryable=False,
        action="choose a model that can obey response_format=json_object and schema-exact governor outputs",
    )


def _ensure_anthropic_messages_compat_ready(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
    *,
    base_url: str | None,
    error_code: str,
    probe_label: str,
    system_in_messages: bool,
    retryable: bool,
    action: str,
) -> None:
    base = normalize_url(base_url)
    if base is None:
        raise ModelProviderCapabilityError(
            error_code=error_code,
            message="Anthropic Messages compatibility route has no endpoint configured.",
            route=route.route,
            probe=f"{probe_label}_compat",
            endpoint=None,
            reason="missing_route_endpoint",
            retryable=True,
            action=action,
        )
    messages_url = base + "/v1/messages"
    tool_result = host._http_json(
        "POST",
        messages_url,
        request_body=anthropic_tool_probe_body(
            _agent_model(host),
            system_in_messages=system_in_messages,
        ),
    )
    if not is_success(tool_result) or not anthropic_content_has_tool_use(tool_result.body_json):
        _raise_capability_error(
            error_code=error_code,
            message="Anthropic Messages endpoint failed the Claude Code tool compatibility probe.",
            route=route,
            probe=f"{probe_label}_tool_compat",
            result=tool_result,
            endpoint=base_url,
            retryable=retryable,
            action=action,
        )
    _ensure_anthropic_streaming_compat(
        host,
        route,
        messages_url=messages_url,
        endpoint=base_url,
        error_code=error_code,
        probe_label=probe_label,
        retryable=retryable,
        action=action,
    )


def _ensure_anthropic_streaming_compat(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
    *,
    messages_url: str,
    endpoint: str | None,
    error_code: str,
    probe_label: str,
    retryable: bool,
    action: str,
) -> None:
    stream_result = host._http_request(
        "POST",
        messages_url,
        request_body={
            "model": _agent_model(host),
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        },
        accept="text/event-stream",
    )
    stream_text = stream_result.raw_body.decode("utf-8", errors="replace")
    if is_success(stream_result) and "event:" in stream_text and "event: error" not in stream_text:
        return
    _raise_capability_error(
        error_code=error_code,
        message="Anthropic Messages endpoint failed the Claude Code streaming compatibility probe.",
        route=route,
        probe=f"{probe_label}_streaming",
        result=stream_result,
        endpoint=endpoint,
        retryable=retryable,
        action=action,
    )


def _post_vllm_chat(
    host: ModelProviderProbeHost,
    route: ModelProviderRoute,
    request_body: Mapping[str, object],
) -> HttpProbeResult:
    endpoint = provider_v1_api_base(route.provider_endpoint)
    if endpoint is None:
        return HttpProbeResult(status_code=None, duration_ms=0, reason="missing_provider_endpoint")
    return host._http_json(
        "POST",
        endpoint.rstrip("/") + "/chat/completions",
        request_body=request_body,
    )


def _agent_model(host: ModelProviderProbeHost) -> str:
    value = getattr(host.settings, "agent_model", None)
    return value.strip() if isinstance(value, str) and value.strip() else "agent-gov-model"


def _raise_capability_error(
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
        reason=result.reason,
        status_code=result.status_code,
        duration_ms=result.duration_ms,
        retryable=retryable,
        action=action,
    )
