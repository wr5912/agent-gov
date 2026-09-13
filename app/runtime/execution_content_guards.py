"""受治理候选写入的结构化 Harness 安全护栏。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT
from pydantic import TypeAdapter, ValidationError

from app.runtime.errors import FeedbackStoreError
from app.runtime.json_types import JsonObject

_JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)

# permission_mode 不是线性的“安全等级”：explore 会拒绝写操作但仍可 ASK，
# dont_ask 会把 ASK 变成 DENY 但并不额外限制已显式允许的写操作。因此用显式的
# 收紧关系表达可接受迁移，避免在不可比模式之间隐式放权。
_PERMISSION_MODE_TIGHTENING_TRANSITIONS = {
    "explore": frozenset({"explore"}),
    "dont_ask": frozenset({"dont_ask"}),
    "default": frozenset({"default", "explore", "dont_ask"}),
    "accept_edits": frozenset({"accept_edits", "default", "explore", "dont_ask"}),
}


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
    if agent.get("runtime") != "agentscope" or agent.get("runtime_contract") != AGENTSCOPE_RUNTIME_CONTRACT:
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
    _guard_permission_mode(new, old)
    ask_tools = policy.get("ask_tools", [])
    if not isinstance(ask_tools, list) or any(not isinstance(item, str) or item != item.strip() or not item or "\0" in item for item in ask_tools):
        raise ExecutionContentGuardError("agent.yaml workspace_policy.ask_tools 必须是合法字符串列表")
    if any(item.partition("(")[0].startswith("mcp__") and any(character in item.partition("(")[0] for character in "*?[") for item in ask_tools):
        raise ExecutionContentGuardError("agent.yaml workspace_policy.ask_tools 不得通配 MCP 工具")
    removed_denies = set(_strings(old_policy.get("denied_tools"))) - set(_strings(policy.get("denied_tools")))
    added_allows = set(_strings(policy.get("allowed_tools"))) - set(_strings(old_policy.get("allowed_tools")))
    old_ask_tools = _strings(old_policy.get("ask_tools"))
    if removed_denies or added_allows or ask_tools != old_ask_tools:
        raise ExecutionContentGuardError("自动改进不得扩大工具权限；请通过人工批准的专用变更流程")
    if set(_strings(policy.get("writable_paths"))) - set(_strings(old_policy.get("writable_paths"))):
        raise ExecutionContentGuardError("自动改进不得扩大 Runtime 可写路径")
    if set(_strings(old_policy.get("denied_read_paths"))) - set(_strings(policy.get("denied_read_paths"))):
        raise ExecutionContentGuardError("自动改进不得缩小 Runtime 拒绝读取路径")
    if set(_strings(old_policy.get("immutable_paths"))) - set(_strings(policy.get("immutable_paths"))):
        raise ExecutionContentGuardError("自动改进不得缩小 Runtime 不可变路径")
    if set(_strings(policy.get("allowed_network_domains"))) - set(_strings(old_policy.get("allowed_network_domains"))):
        raise ExecutionContentGuardError("自动改进不得扩大 Runtime 网络访问范围")
    _guard_sandbox(policy, old_policy)
    if old and new.get("runtime_middlewares") != old.get("runtime_middlewares"):
        raise ExecutionContentGuardError("自动改进不得修改 Runtime middleware 链")


def _guard_permission_mode(new: JsonObject, old: JsonObject) -> None:
    session = _object(new.get("session"))
    mode = session.get("permission_mode", "default")
    if not isinstance(mode, str) or mode not in _PERMISSION_MODE_TIGHTENING_TRANSITIONS:
        raise ExecutionContentGuardError("agent.yaml session.permission_mode 非法或不安全")
    if session.get("model_profile", "default") != "default":
        raise ExecutionContentGuardError("agent.yaml 只能使用受治理的 default model_profile")
    if not old:
        return
    old_session = _object(old.get("session"))
    old_mode = old_session.get("permission_mode", "default")
    if not isinstance(old_mode, str) or old_mode not in _PERMISSION_MODE_TIGHTENING_TRANSITIONS:
        raise ExecutionContentGuardError("原 agent.yaml session.permission_mode 非法，不能自动修改")
    if mode not in _PERMISSION_MODE_TIGHTENING_TRANSITIONS[old_mode]:
        raise ExecutionContentGuardError("自动改进不得放宽 Runtime permission_mode")
    if session.get("cwd", ".") != old_session.get("cwd", "."):
        raise ExecutionContentGuardError("自动改进不得修改 Runtime session.cwd")


def _guard_sandbox(policy: JsonObject, old_policy: JsonObject) -> None:
    old_sandbox = old_policy.get("sandbox")
    new_sandbox = policy.get("sandbox")
    if old_sandbox is None and new_sandbox is None:
        return
    if not isinstance(new_sandbox, dict):
        raise ExecutionContentGuardError("自动改进不得移除 Runtime sandbox")
    required = {
        "enabled": True,
        "fail_if_unavailable": True,
        "allow_unsandboxed_commands": False,
    }
    if new_sandbox != required:
        raise ExecutionContentGuardError("自动改进不得放宽 Runtime sandbox")


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
