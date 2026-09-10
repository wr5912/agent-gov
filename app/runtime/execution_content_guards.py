"""受治理候选写入的结构化 Harness 安全护栏。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

from app.runtime.errors import FeedbackStoreError
from app.runtime.json_types import JsonObject

_JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)


class ExecutionContentGuardError(FeedbackStoreError):
    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = "EXECUTION_CONTENT_GUARD_ERROR"


def guard_execution_write(*, target_path: str, new_bytes: bytes, original_bytes: bytes | None) -> None:
    normalized = Path(target_path).as_posix()
    if normalized == "agent.yaml" or normalized.endswith("/agent.yaml"):
        _guard_manifest(target_path, new_bytes, original_bytes)
    elif normalized.startswith("mcp/") and normalized.endswith(".json"):
        _guard_mcp(target_path, new_bytes, original_bytes)


def _guard_manifest(target_path: str, new_bytes: bytes, original_bytes: bytes | None) -> None:
    new = _yaml_object(target_path, new_bytes)
    old = _yaml_object(target_path, original_bytes) if original_bytes else {}
    agent = _object(new.get("agent"))
    policy = _object(new.get("workspace_policy"))
    if new.get("schema_version") != 1:
        raise ExecutionContentGuardError("agent.yaml schema_version 必须保持为 1")
    if agent.get("runtime") != "agentscope" or agent.get("runtime_contract") != "agentscope-app/2.0.8":
        raise ExecutionContentGuardError("agent.yaml 不得脱离固定 AgentScope Runtime 契约")
    if policy.get("fail_closed") is not True or policy.get("immutable_harness") is not True:
        raise ExecutionContentGuardError("agent.yaml 不得关闭 fail_closed 或 immutable_harness")
    if policy.get("allow_for_run") is not False:
        raise ExecutionContentGuardError("agent.yaml 不得开启整轮持久放权")
    old_agent = _object(old.get("agent"))
    for field in ("id", "runtime", "runtime_contract"):
        if field in old_agent and agent.get(field) != old_agent.get(field):
            raise ExecutionContentGuardError(f"agent.yaml 不得修改不可变身份字段: agent.{field}")
    old_policy = _object(old.get("workspace_policy"))
    removed_denies = set(_strings(old_policy.get("denied_tools"))) - set(_strings(policy.get("denied_tools")))
    added_allows = set(_strings(policy.get("allowed_tools"))) - set(_strings(old_policy.get("allowed_tools")))
    if removed_denies or added_allows:
        raise ExecutionContentGuardError("自动改进不得扩大工具权限；请通过人工批准的专用变更流程")
    if set(_strings(policy.get("writable_paths"))) - set(_strings(old_policy.get("writable_paths"))):
        raise ExecutionContentGuardError("自动改进不得扩大 Runtime 可写路径")


def _guard_mcp(target_path: str, new_bytes: bytes, original_bytes: bytes | None) -> None:
    new = _json_object(target_path, new_bytes)
    old = _json_object(target_path, original_bytes) if original_bytes else {}
    config = _object(new.get("mcp_config"))
    if config.get("type") not in {"http_mcp", "stdio_mcp"}:
        raise ExecutionContentGuardError(f"{target_path} 的 mcp_config.type 非法")
    if not isinstance(new.get("credential_refs"), list):
        raise ExecutionContentGuardError(f"{target_path} 必须声明 credential_refs")
    if config.get("type") == "stdio_mcp" and _object(old.get("mcp_config")) != config:
        raise ExecutionContentGuardError("自动改进不得新增或修改可启动进程的 stdio MCP")


def _yaml_object(path: str, raw: bytes | None) -> JsonObject:
    try:
        loaded = yaml.safe_load((raw or b"").decode("utf-8")) or {}
        return _JSON_OBJECT_ADAPTER.validate_python(loaded)
    except (UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ExecutionContentGuardError(f"{path} 不是合法 YAML: {exc.__class__.__name__}") from exc


def _json_object(path: str, raw: bytes | None) -> JsonObject:
    try:
        loaded = json.loads((raw or b"").decode("utf-8"))
        return _JSON_OBJECT_ADAPTER.validate_python(loaded)
    except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ExecutionContentGuardError(f"{path} 不是合法 JSON: {exc.__class__.__name__}") from exc


def _object(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
