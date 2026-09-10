"""AgentGov 查询 Langfuse 的最小只读客户端。

Trace 创建、Span 和 flush 由独立 AgentScope Runtime 负责；控制面只按已保存的
OTel ``trace_id`` 读取观测结果。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from ..json_types import JsonObject
from ..settings import AppSettings


class RuntimeLangfuseClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def fetch_trace(self, trace_id: str) -> Optional[JsonObject]:
        if not trace_id or not self.settings.langfuse_enabled:
            return None
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            return None
        try:
            from langfuse.api.client import LangfuseAPI

            client = LangfuseAPI(
                base_url=self.settings.langfuse_base_url,
                username=self.settings.langfuse_public_key,
                password=self.settings.langfuse_secret_key,
                x_langfuse_public_key=self.settings.langfuse_public_key,
                timeout=10,
            )
            # Runtime 已在 OTLP 出口做 allowlist；查询侧同样不请求 Langfuse 的
            # prompt/output I/O 字段，避免其他写入方的正文越过控制面边界。
            trace = client.trace.get(trace_id, fields="core,scores,observations,metrics")
            plain = _to_plain(trace)
            return plain if isinstance(plain, dict) else {"value": plain}
        except Exception as exc:  # 查询失败要作为证据状态返回，不能伪装 404 或回显上游正文。
            return {"fetch_status": "failed", "error_type": exc.__class__.__name__}


def _to_plain(value: Any) -> Any:
    """将 Langfuse SDK 响应转为 JSON-safe 值，不依赖任一 Agent Runtime 消息模型。"""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_to_plain(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_plain(model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _to_plain(asdict(value))
    if hasattr(value, "__dict__"):
        return {key: _to_plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)
