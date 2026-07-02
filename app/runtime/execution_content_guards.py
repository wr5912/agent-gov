"""受治理 apply 写入结构化配置文件的安全护栏（Phase 2 拓宽写目标后的必备防护）。

优化能改的目标从仅 CLAUDE.md 拓宽到 .claude/settings.json / .mcp.json 后，任何写入这些结构化配置的
操作在 governed apply（隔离 worktree）内必须先过本护栏，违规抛 ExecutionContentGuardError →
上层 abandon change set + 回退启发式，绝不落盘损坏或提权。

- JSON 合法性：settings.json / .mcp.json 写入结果必须可解析。
- 权限升级防护：settings.json 的 permissions 不得新增危险 allow（无约束的高危工具授权 / 通配 MCP），
  也不得删除既有 deny（deny 单调，只能加不能减）。
- CLAUDE.md / SKILL.md 等非结构化配置不拦（内容语义由 governor 负责，写入仍受 sha256 乐观锁 + 隔离约束）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.runtime.errors import FeedbackStoreError

_HIGH_RISK_TOOLS = {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "Read"}
# 无约束参数：整工具授权或 *、**、根 /** ——workspace 相对的 ./** 不算（Agent 正常工作范围）。
_UNRESTRICTED_ARGS = {"", "*", "**", "/**"}
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
    """对结构化配置文件的写入做合法性 + 权限升级防护；违规抛 ExecutionContentGuardError。"""
    name = Path(target_path).name
    if _is_settings_json(target_path):
        _guard_settings_json(target_path, new_bytes, original_bytes)
    elif name == ".mcp.json":
        _require_valid_json(target_path, new_bytes)
    # 其它文件（CLAUDE.md / SKILL.md / rules 等）不做结构化护栏。


def _is_settings_json(target_path: str) -> bool:
    path = Path(target_path)
    return path.name == "settings.json" and path.parent.name == ".claude"


def _guard_settings_json(target_path: str, new_bytes: bytes, original_bytes: bytes | None) -> None:
    new_data = _require_valid_json(target_path, new_bytes)
    old_data = _parse_json(original_bytes) if original_bytes is not None else {}
    new_perms = _permission_lists(new_data)
    old_perms = _permission_lists(old_data)

    removed_deny = set(old_perms["deny"]) - set(new_perms["deny"])
    if removed_deny:
        raise ExecutionContentGuardError(f"settings.json 写入删除了既有 deny 规则（禁止降权）: {sorted(removed_deny)}")

    added_allow = set(new_perms["allow"]) - set(old_perms["allow"])
    dangerous = sorted(entry for entry in added_allow if _is_dangerous_allow(entry))
    if dangerous:
        raise ExecutionContentGuardError(f"settings.json 写入新增危险 allow（禁止提权）: {dangerous}")


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


def _permission_lists(data: Any) -> dict[str, list[str]]:
    permissions = data.get("permissions") if isinstance(data, dict) else None
    result: dict[str, list[str]] = {key: [] for key in _PERMISSION_KEYS}
    if isinstance(permissions, dict):
        for key in _PERMISSION_KEYS:
            value = permissions.get(key)
            if isinstance(value, list):
                result[key] = [str(item) for item in value]
    return result


def _is_dangerous_allow(entry: str) -> bool:
    entry = entry.strip()
    if entry.startswith("mcp__*"):  # 通配 MCP 授权
        return True
    match = _ENTRY_RE.match(entry)
    if not match:
        return False
    tool, arg = match.group(1), (match.group(2) or "").strip()
    return tool in _HIGH_RISK_TOOLS and arg in _UNRESTRICTED_ARGS
