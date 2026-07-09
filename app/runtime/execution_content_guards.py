"""受治理 apply 写入结构化配置文件的安全护栏（Phase 2 拓宽写目标后的必备防护）。

优化能改的目标从仅 CLAUDE.md 拓宽到 .claude/settings.json / .mcp.json（+ 现存 skills）后，写入这些
结构化配置必须先过本护栏；违规抛 ExecutionContentGuardError → 上层 abandon change set + 回退启发式。
本护栏是「被 grounding 注入/劫持的 governor」这一威胁的纵深防线，采用「默认危险、显式安全」的保守判定。

- 覆盖：settings.json 与 settings.local.json（Claude Code 后者优先级更高）、.mcp.json 与 .mcp.local.json。
- settings 护栏：JSON 合法；deny 单调（只加不减）；governed apply 不得新增危险 allow（高危执行工具的任意授权、
  mcp 通配、ask→allow 迁移）；不得变更 hooks / env、不得开启 enableAllProjectMcpServers、
  defaultMode 不得升为 bypassPermissions/acceptEdits、additionalDirectories 不得新增、
  不得移除 disableBypassPermissionsMode。
- .mcp.json 护栏：JSON 合法；不得新增或变更 command/stdio 型 MCP server（任意代码执行面）。
- 真正的路径边界由 applier 的 allowed_targets allowlist 强制（本护栏只对结构化文件做内容语义校验）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.runtime.errors import FeedbackStoreError

# 不得经受治理 apply 自动新增到 allow 的高危执行工具；seed/operator 基线另行受模板审查约束。
_HIGH_EXEC_TOOLS = {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "Task"}
# 非高危工具（Read/Glob/Grep 等）的显式全域通配参数视为危险；workspace 相对 ./** 与裸工具名（如 Grep）不算。
_UNRESTRICTED_ARGS = {"*", "**", "/**"}
_WEAK_DEFAULT_MODES = {"bypassPermissions", "acceptEdits"}
_SETTINGS_NAMES = {"settings.json", "settings.local.json"}
_MCP_NAMES = {".mcp.json", ".mcp.local.json"}
_PERMISSION_KEYS = ("allow", "ask", "deny")
_ENTRY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\((.*)\))?$")


class ExecutionContentGuardError(FeedbackStoreError):
    """写入结构化配置违反合法性/提权护栏（route-safe，409）。"""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = "EXECUTION_CONTENT_GUARD_ERROR"


def guard_execution_write(*, target_path: str, new_bytes: bytes, original_bytes: bytes | None) -> None:
    """对结构化配置文件的写入做合法性 + 提权防护；违规抛 ExecutionContentGuardError。"""
    name = Path(target_path).name
    if _is_settings_json(target_path):
        _guard_settings_json(target_path, new_bytes, original_bytes)
    elif name in _MCP_NAMES:
        _guard_mcp_json(target_path, new_bytes, original_bytes)
    # 其它文件（CLAUDE.md / SKILL.md / rules）不做结构化护栏，路径由 applier allowlist 收敛。


def _is_settings_json(target_path: str) -> bool:
    path = Path(target_path)
    return path.name in _SETTINGS_NAMES and path.parent.name == ".claude"


# ---- settings.json / settings.local.json ----

def _guard_settings_json(target_path: str, new_bytes: bytes, original_bytes: bytes | None) -> None:
    new_data = _require_valid_json(target_path, new_bytes)
    old_data = _parse_json(original_bytes)

    _guard_settings_top_keys(old_data, new_data, target_path)

    new_perm = _perm_obj(new_data)
    old_perm = _perm_obj(old_data)
    _guard_permission_posture(old_perm, new_perm, target_path)

    new_lists = _permission_lists(new_data)
    old_lists = _permission_lists(old_data)
    removed_deny = set(old_lists["deny"]) - set(new_lists["deny"])
    if removed_deny:
        raise ExecutionContentGuardError(f"settings 写入删除了既有 deny 规则（禁止降权）: {sorted(removed_deny)}")

    added_allow = set(new_lists["allow"]) - set(old_lists["allow"])
    old_ask = set(old_lists["ask"])
    dangerous = sorted(entry for entry in added_allow if _is_dangerous_allow(entry) or entry in old_ask)
    if dangerous:
        raise ExecutionContentGuardError(f"settings 写入新增危险/提权 allow（禁止）: {dangerous}")


def _guard_settings_top_keys(old_data: Any, new_data: Any, target_path: str) -> None:
    # hooks / env：任意命令执行面与运行环境，governed apply 不得新增或变更。
    for key in ("hooks", "env"):
        if _get(new_data, key) != _get(old_data, key):
            raise ExecutionContentGuardError(f"settings 写入变更了 {key}（{target_path}）——禁止 governed apply 修改该高危键")
    if _get(new_data, "enableAllProjectMcpServers") is True and _get(old_data, "enableAllProjectMcpServers") is not True:
        raise ExecutionContentGuardError("settings 写入开启 enableAllProjectMcpServers（禁止自动放开全部项目 MCP）")


def _guard_permission_posture(old_perm: dict[str, Any], new_perm: dict[str, Any], target_path: str) -> None:
    new_mode = new_perm.get("defaultMode")
    if isinstance(new_mode, str) and new_mode in _WEAK_DEFAULT_MODES and new_mode != old_perm.get("defaultMode"):
        raise ExecutionContentGuardError(f"settings.permissions.defaultMode 升为 {new_mode}（禁止弱化权限姿态）")
    added_dirs = set(_str_list(new_perm.get("additionalDirectories"))) - set(_str_list(old_perm.get("additionalDirectories")))
    if added_dirs:
        raise ExecutionContentGuardError(f"settings.permissions.additionalDirectories 新增 {sorted(added_dirs)}（禁止扩大目录访问）")
    if old_perm.get("disableBypassPermissionsMode") == "disable" and new_perm.get("disableBypassPermissionsMode") != "disable":
        raise ExecutionContentGuardError("settings 写入移除/弱化了 disableBypassPermissionsMode（禁止）")


def _is_dangerous_allow(entry: str) -> bool:
    entry = entry.strip()
    if entry.startswith("mcp__") and "*" in entry:  # 任意 MCP 通配（含 server 级 mcp__server__*）
        return True
    match = _ENTRY_RE.match(entry)
    if not match:
        return True  # 解析不了 → 保守判危险
    tool, arg = match.group(1), (match.group(2) or "").strip()
    if tool in _HIGH_EXEC_TOOLS:  # 高危执行工具的任何 allow 都视为提权
        return True
    return arg in _UNRESTRICTED_ARGS


# ---- .mcp.json ----

def _guard_mcp_json(target_path: str, new_bytes: bytes, original_bytes: bytes | None) -> None:
    new_data = _require_valid_json(target_path, new_bytes)
    old_data = _parse_json(original_bytes)
    new_servers = _servers(new_data)
    old_servers = _servers(old_data)
    for name, config in new_servers.items():
        if not isinstance(config, dict):
            continue
        old_config = old_servers.get(name)
        old_config = old_config if isinstance(old_config, dict) else None
        if not _is_command_server(config):
            continue
        # 新增或变更 command/stdio 型 server（command/args/type）——任意代码执行面。
        if (
            old_config is None
            or config.get("command") != old_config.get("command")
            or config.get("args") != old_config.get("args")
            or config.get("type") != old_config.get("type")
        ):
            raise ExecutionContentGuardError(f".mcp.json 新增/变更 command/stdio 型 MCP server '{name}'（禁止：任意代码执行面）")


def _is_command_server(config: dict[str, Any]) -> bool:
    if config.get("command"):
        return True
    server_type = config.get("type")
    return isinstance(server_type, str) and server_type.lower() == "stdio"


def _servers(data: Any) -> dict[str, Any]:
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


# ---- helpers ----

def _require_valid_json(target_path: str, new_bytes: bytes) -> Any:
    try:
        return json.loads(new_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionContentGuardError(f"{target_path} 写入不是合法 JSON: {exc.__class__.__name__}") from exc


def _parse_json(raw: bytes | None) -> Any:
    if raw is None:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}


def _get(data: Any, key: str) -> Any:
    return data.get(key) if isinstance(data, dict) else None


def _perm_obj(data: Any) -> dict[str, Any]:
    permissions = data.get("permissions") if isinstance(data, dict) else None
    return permissions if isinstance(permissions, dict) else {}


def _permission_lists(data: Any) -> dict[str, list[str]]:
    permissions = _perm_obj(data)
    result: dict[str, list[str]] = {key: [] for key in _PERMISSION_KEYS}
    for key in _PERMISSION_KEYS:
        value = permissions.get(key)
        if isinstance(value, list):
            result[key] = [str(item) for item in value]
    return result


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
