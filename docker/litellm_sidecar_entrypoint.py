from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

CONFIG_PATH = Path("/tmp/agent-gov-litellm-config.yaml")
LITELLM_NO_MASTER_KEY_WARNING = (
    "LITELLM_MASTER_KEY is not set! All requests will be treated as INTERNAL_USER with no admin access. Set LITELLM_MASTER_KEY for production use."
)


class IntentionalNoMasterKeyFilter(logging.Filter):
    """只过滤与 AgentGov 内部 sidecar 信任边界冲突的 LiteLLM 固定告警。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return LITELLM_NO_MASTER_KEY_WARNING not in record.getMessage()


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


def build_sidecar_config(
    *,
    backend: str,
    model_name: str,
    provider_api_url: str,
    provider_api_key: str,
) -> dict[str, object]:
    return {
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


def run_litellm_proxy(config_path: Path) -> None:
    # LiteLLM 1.88.1 在不配置代理管理员密钥时无条件打印 CRITICAL。AgentGov 的 sidecar
    # 不发布宿主机端口，且 Compose 明确关闭管理 UI/文档；这里仅过滤该固定文案，真实故障不降级。
    from litellm import run_server

    logging.getLogger("LiteLLM Proxy").addFilter(IntentionalNoMasterKeyFilter())
    run_server.main(
        args=["--config", str(config_path), "--host", "0.0.0.0", "--port", "4000"],
        prog_name="litellm",
    )


def write_sidecar_config(config: dict[str, object], path: Path = CONFIG_PATH) -> None:
    """写入只供 sidecar 进程读取的含凭据配置。"""

    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


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
    config = build_sidecar_config(
        backend=backend,
        model_name=model_name,
        provider_api_url=provider_api_url,
        provider_api_key=provider_api_key,
    )
    write_sidecar_config(config)
    print(
        "event=MODEL_PROVIDER_SIDECAR_CONFIGURED "
        f"backend={backend} provider_endpoint={_sanitize_endpoint(provider_api_url)} model={model_name} "
        "access=compose_internal admin_ui=disabled proxy_master_key=not_required",
        flush=True,
    )
    run_litellm_proxy(CONFIG_PATH)


if __name__ == "__main__":
    main()
