"""AgentGov 查询 Langfuse 的最小只读客户端。

Trace 创建、Span 和 flush 由独立 AgentScope Runtime 负责；控制面只按已保存的
OTel ``trace_id`` 读取观测结果。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from ..json_types import JsonObject
from ..settings import AppSettings

_MAX_OBSERVATIONS = 10_000
_MAX_ATTRIBUTE_ITEMS = 512
_MAX_USAGE_ITEMS = 64
_USAGE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_SAFE_OBSERVATION_NAMES = frozenset(
    {
        "agentgov.run",
        "agentgov.run.stage",
        "agentscope.operation",
        "chat",
        "execute_tool",
        "invoke_agent",
    }
)
_SAFE_OBSERVATION_TYPES = frozenset({"EVENT", "GENERATION", "SPAN"})
_SAFE_LEVELS = frozenset({"DEFAULT", "DEBUG", "ERROR", "WARNING"})
_SAFE_STATUSES = frozenset({"ERROR", "FAILED", "OK", "SUCCESS"})
_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "agentgov.run.id",
        "agentgov.run.stage",
        "agentgov.run.finished_reason",
        "agentgov.agent.id",
        "agentgov.agent.version_id",
        "agentgov.harness.digest",
        "agentgov.runtime.version",
        "agentgov.content.input.length",
        "agentgov.content.input.sha256",
        "agentgov.content.output.length",
        "agentgov.content.output.sha256",
        "agentgov.content.tool_definitions.length",
        "agentgov.content.tool_definitions.sha256",
        "agentgov.content.tool_arguments.length",
        "agentgov.content.tool_arguments.sha256",
        "agentgov.content.tool_result.length",
        "agentgov.content.tool_result.sha256",
        "agentgov.event.id",
        "agentgov.event.type",
        "agentgov.receipt.id",
        "agentscope.agent.id",
        "agentscope.runtime.version",
        "agentscope.session.id",
        "agentscope.agent.reply_id",
        "agentscope.agent.hitl_pending_tools",
        "agentscope.agent.hitl_pending_tool_call_ids",
        "agentscope.agent.external_execution_pending_tools",
        "agentscope.agent.external_execution_pending_tool_call_ids",
        "agentscope.agent.incoming_event_type",
        "agentscope.agent.is_external_execution",
        "agentscope.tool.result.state",
        "agentscope.usage.cache_input_tokens",
        "agentscope.usage.cache_creation_input_tokens",
        "gen_ai.conversation.id",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.id",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.agent.id",
        "gen_ai.tool.call.id",
        "gen_ai.tool.name",
        "tool.name",
        "mcp.server.name",
        "mcp.connection.status",
    }
)
_PARENT_FIELDS = (
    "parent_observation_id",
    "parentObservationId",
    "parent_span_id",
    "parentSpanId",
    "parent_id",
    "parentId",
)


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

            with httpx.Client(timeout=10, trust_env=False) as http_client:
                client = LangfuseAPI(
                    base_url=self.settings.langfuse_base_url,
                    username=self.settings.langfuse_public_key,
                    password=self.settings.langfuse_secret_key,
                    x_langfuse_public_key=self.settings.langfuse_public_key,
                    timeout=10,
                    httpx_client=http_client,
                )
                # fields 只缩小上游读取面，不能当成隐私边界。其他写入方仍可能在 core
                # 或 observation 中放入正文，因此返回前必须再次执行正向字段投影。
                trace = client.trace.get(trace_id, fields="core,observations")
            return project_validation_trace(trace)
        except Exception as exc:  # 查询失败要作为证据状态返回，不能伪装 404 或回显上游正文。
            return {"fetch_status": "failed", "error_type": exc.__class__.__name__}


def project_validation_trace(value: Any) -> JsonObject:
    """投影供完整性校验使用的瞬时视图，丢弃所有正文和未批准 metadata。"""

    plain = _to_plain(value)
    if not isinstance(plain, dict):
        return {"fetch_status": "invalid"}

    projected: JsonObject = {}
    _copy_text(plain, projected, "id", max_length=256)
    _copy_text(plain, projected, "trace_id", max_length=256)
    _copy_enum(plain, projected, "name", _SAFE_OBSERVATION_NAMES)
    _copy_text(plain, projected, "timestamp", max_length=64)
    trace_path = _trace_path(plain.get("url") or plain.get("html_path") or plain.get("htmlPath"))
    if trace_path:
        projected["url"] = trace_path
    _copy_attribute_containers(plain, projected)

    observations = plain.get("observations")
    if observations is not None:
        if not isinstance(observations, list) or len(observations) > _MAX_OBSERVATIONS:
            projected["observations"] = []
        else:
            projected["observations"] = [item for observation in observations if (item := _project_observation(observation)) is not None]
            if len(projected["observations"]) != len(observations):
                projected["observations"] = []
    return projected


def _project_observation(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    projected: JsonObject = {}
    for key in ("id", "observation_id", "trace_id", "traceId"):
        _copy_text(value, projected, key, max_length=256)
    for key in _PARENT_FIELDS:
        if value.get(key) is None and key in value:
            projected[key] = None
        else:
            _copy_text(value, projected, key, max_length=256)
    _copy_enum(value, projected, "name", _SAFE_OBSERVATION_NAMES)
    _copy_enum(value, projected, "type", _SAFE_OBSERVATION_TYPES)
    _copy_enum(value, projected, "level", _SAFE_LEVELS)
    _copy_enum(value, projected, "status", _SAFE_STATUSES)
    for key in ("start_time", "startTime", "end_time", "endTime"):
        _copy_text(value, projected, key, max_length=64)
    _copy_attribute_containers(value, projected)
    operation_name = _consistent_operation_name(value)
    if operation_name is not None:
        projected_name = projected.get("name")
        if projected_name is not None and projected_name != operation_name:
            return None
        # Langfuse may expose a concrete tool name even though the redacted OTel
        # span name is ``execute_tool``. Discard that upstream label and retain
        # only the approved semantic operation.
        projected["name"] = operation_name
    for key in ("usage", "usage_details", "usageDetails"):
        usage = _safe_usage(value.get(key))
        if usage:
            projected[key] = usage
    return projected


def _consistent_operation_name(source: Mapping[str, object]) -> str | None:
    """只用一致且已批准的 OTel operation 补齐 Langfuse 空 observation name。"""

    values: list[object] = []
    for attributes in _attribute_containers(source):
        if "gen_ai.operation.name" in attributes:
            values.append(attributes["gen_ai.operation.name"])
    if not values or any(type(value) is not type(values[0]) or value != values[0] for value in values[1:]):
        return None
    value = values[0]
    return value if isinstance(value, str) and value in _SAFE_OBSERVATION_NAMES else None


def _attribute_containers(source: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    containers: list[Mapping[str, object]] = [source]
    attributes = source.get("attributes")
    if isinstance(attributes, Mapping):
        containers.append(attributes)
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
        nested_attributes = metadata.get("attributes")
        if isinstance(nested_attributes, Mapping):
            containers.append(nested_attributes)
    return tuple(containers)


def _copy_attribute_containers(source: Mapping[str, object], target: JsonObject) -> None:
    direct = _safe_attributes(source)
    target.update(direct)

    attributes = source.get("attributes")
    if isinstance(attributes, Mapping):
        target["attributes"] = _safe_attributes(attributes)

    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return
    safe_metadata = _safe_attributes(metadata)
    nested_attributes = metadata.get("attributes")
    if isinstance(nested_attributes, Mapping):
        safe_metadata["attributes"] = _safe_attributes(nested_attributes)
    if safe_metadata:
        target["metadata"] = safe_metadata


def _safe_attributes(source: Mapping[str, object]) -> JsonObject:
    safe: JsonObject = {}
    for key in _SAFE_ATTRIBUTE_KEYS:
        if key not in source:
            continue
        value = _safe_attribute_value(source[key])
        if value is not None:
            safe[key] = value
    return safe


def _safe_attribute_value(value: object) -> Any | None:
    if isinstance(value, bool | str):
        return value if not isinstance(value, str) or len(value) <= 2048 else None
    if isinstance(value, int):
        return value if -(1 << 63) <= value <= (1 << 63) - 1 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list | tuple) and len(value) <= _MAX_ATTRIBUTE_ITEMS:
        items = [_safe_attribute_value(item) for item in value]
        return items if all(item is not None for item in items) else None
    return None


def _safe_usage(value: object) -> JsonObject:
    if not isinstance(value, Mapping) or len(value) > _MAX_USAGE_ITEMS:
        return {}
    safe: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _USAGE_KEY.fullmatch(key) or isinstance(item, bool):
            continue
        if isinstance(item, int) and 0 <= item <= (1 << 63) - 1 or isinstance(item, float) and math.isfinite(item) and item >= 0:
            safe[key] = item
    return safe


def _copy_text(source: Mapping[str, object], target: JsonObject, key: str, *, max_length: int) -> None:
    value = source.get(key)
    if isinstance(value, str) and value and len(value) <= max_length:
        target[key] = value


def _copy_enum(source: Mapping[str, object], target: JsonObject, key: str, allowed: frozenset[str]) -> None:
    value = source.get(key)
    if isinstance(value, str) and value in allowed:
        target[key] = value


def _trace_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    path = parsed.path
    if "/traces/" not in path or not path.startswith("/"):
        return None
    return path


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
