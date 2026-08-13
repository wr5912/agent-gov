#!/usr/bin/env python3
"""Claude Code PreToolUse hook: protect the Workspace governance surface.

Tool capability policy belongs to ``.claude/settings.json``. This hook has one
responsibility: reject edits to the checked-in Agent identity and governance
files. It never grants a tool call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn

WORKSPACE = Path(__file__).resolve().parent.parent
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
PATH_KEYS = ("file_path", "path", "notebook_path")
PROTECTED_FILES = frozenset(
    {
        (WORKSPACE / ".mcp.json").resolve(),
        (WORKSPACE / "CLAUDE.md").resolve(),
        (WORKSPACE / "agent.yaml").resolve(),
    }
)
PROTECTED_DIRECTORIES = (
    (WORKSPACE / ".claude").resolve(),
    (WORKSPACE / "hooks").resolve(),
)


def deny(reason: str) -> NoReturn:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0)


def resolve_candidate_path(raw_path: str, cwd: object) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate.resolve()
    anchor = Path(cwd) if isinstance(cwd, str) and cwd.strip() else WORKSPACE
    return (anchor / candidate).resolve()


def is_protected_path(candidate: Path) -> bool:
    if candidate in PROTECTED_FILES:
        return True
    return any(candidate == directory or candidate.is_relative_to(directory) for directory in PROTECTED_DIRECTORIES)


try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    deny("PreToolUse 守卫无法解析工具输入，已阻止。")

if not isinstance(payload, dict):
    deny("PreToolUse 守卫收到非法顶层输入，已阻止。")

tool_name = payload.get("tool_name")
tool_input = payload.get("tool_input")
if not isinstance(tool_name, str) or not tool_name.strip():
    deny("PreToolUse 守卫收到非法工具名称，已阻止。")
if not isinstance(tool_input, dict):
    deny("PreToolUse 守卫收到非法工具参数，已阻止。")

if tool_name in WRITE_TOOLS:
    raw_path = next((tool_input.get(key) for key in PATH_KEYS if tool_input.get(key) is not None), None)
    if not isinstance(raw_path, str) or not raw_path.strip():
        deny("写工具缺少有效目标路径，已阻止。")
    try:
        candidate_path = resolve_candidate_path(raw_path, payload.get("cwd"))
    except (OSError, RuntimeError, ValueError):
        deny("写工具目标路径无效，已阻止。")
    if is_protected_path(candidate_path):
        deny("业务 Agent 治理文件由 AgentGov 管理，禁止在会话中修改。")

# No decision means continue with the native permission policy. This hook never allows.
raise SystemExit(0)
