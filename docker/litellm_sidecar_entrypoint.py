from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

CONFIG_PATH = Path("/tmp/agent-gov-litellm-config.yaml")


def _clean_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().rstrip("/")
    return stripped or None


def _openai_api_base(value: str) -> str:
    base = _clean_base_url(value) or value
    return base if base.endswith("/v1") else f"{base}/v1"


def _sanitize_endpoint(value: str | None) -> str | None:
    endpoint = _clean_base_url(value)
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


def _model_params(*, backend: str, model_name: str, provider_api_url: str, provider_api_key: str) -> dict[str, str]:
    if backend == "ollama":
        return {
            "model": f"ollama/{model_name}",
            "api_base": provider_api_url,
            "api_key": provider_api_key or "none",
        }
    return {
        "model": f"openai/{model_name}",
        "api_base": _openai_api_base(provider_api_url),
        "api_key": provider_api_key or "none",
    }


def main() -> None:
    provider_api_url = _clean_base_url(os.environ.get("MODEL_PROVIDER_API_URL"))
    if not provider_api_url:
        print(
            "[WARN] event=MODEL_PROVIDER_SIDECAR_IDLE reason=missing_MODEL_PROVIDER_API_URL action=waiting_for_configuration",
            flush=True,
        )
        while True:
            time.sleep(3600)

    backend = (os.environ.get("MODEL_PROVIDER_BACKEND") or "vllm").strip().lower()
    if backend not in {"vllm", "ollama", "openai_compatible"}:
        backend = "openai_compatible"
    model_name = (os.environ.get("AGENT_MODEL") or "agent-gov-model").strip() or "agent-gov-model"
    provider_api_key = os.environ.get("MODEL_PROVIDER_API_KEY") or ""
    config = {
        "model_list": [
            {
                "model_name": model_name,
                "litellm_params": _model_params(
                    backend=backend,
                    model_name=model_name,
                    provider_api_url=provider_api_url,
                    provider_api_key=provider_api_key,
                ),
            }
        ],
        "litellm_settings": {
            "drop_params": True,
            # Claude Code 经 Anthropic /v1/messages 接入；litellm 默认把 OpenAI 类 provider 的
            # /v1/messages 路由到上游 Responses API(/v1/responses)，绕过 vLLM 的 --tool-call-parser，
            # 导致工具调用以文本泄漏、第 2 轮回放上游 /v1/responses 报 400（litellm 官方已知，见
            # anthropic_messages handler 注释）。强制走 /v1/chat/completions——解析器在该端点生效，
            # 多轮正常；模型无关（任意带正确 vLLM tool parser 的模型都适用，无需逐模型适配）。
            "use_chat_completions_url_for_anthropic_messages": True,
        },
        "general_settings": {
            "health_check_details": False,
        },
    }
    CONFIG_PATH.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print(
        "event=MODEL_PROVIDER_SIDECAR_CONFIGURED "
        f"backend={backend} provider_endpoint={_sanitize_endpoint(provider_api_url)} model={model_name}",
        flush=True,
    )
    os.execvp("litellm", ["litellm", "--config", str(CONFIG_PATH), "--host", "0.0.0.0", "--port", "4000"])


if __name__ == "__main__":
    main()
