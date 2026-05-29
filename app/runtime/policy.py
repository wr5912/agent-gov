from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny

from .agent_profiles import AgentRuntimeProfile


DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bkubectl\s+(delete|drain|cordon|apply|patch|replace)\b",
    r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+rm)\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\bcurl\b.*\|\s*(bash|sh)",
    r"\bwget\b.*\|\s*(bash|sh)",
]

READ_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Grep": ("path",),
    "Glob": ("path",),
    "NotebookRead": ("notebook_path",),
}
WRITE_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
}
PATH_TOOL_NAMES = tuple(sorted(set(READ_PATH_FIELDS) | set(WRITE_PATH_FIELDS)))


def _is_dangerous_bash(command: str) -> str | None:
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return pattern
    return None


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_under(path: Path, roots: Iterable[Path]) -> bool:
    resolved_path = _resolved(path)
    for root in roots:
        resolved_root = _resolved(root)
        if resolved_path == resolved_root or resolved_root in resolved_path.parents:
            return True
    return False


def _tool_path(tool_input: dict[str, Any], fields: tuple[str, ...], cwd: Path) -> Path:
    for field in fields:
        raw = tool_input.get(field)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            return candidate if candidate.is_absolute() else cwd / candidate
    return cwd


def _path_policy_denial(tool_name: str, tool_input: dict[str, Any], profile: AgentRuntimeProfile) -> str | None:
    fields = READ_PATH_FIELDS.get(tool_name) or WRITE_PATH_FIELDS.get(tool_name)
    if not fields:
        return None

    target = _tool_path(tool_input, fields, profile.workspace_dir)
    if _is_under(target, profile.denied_paths):
        return f"{tool_name} target is denied for profile {profile.name}: {target}"

    allowed_roots = profile.readable_paths if tool_name in READ_PATH_FIELDS else profile.writable_paths
    if not _is_under(target, allowed_roots):
        return f"{tool_name} target is outside allowed paths for profile {profile.name}: {target}"
    return None


async def guard_tool_use(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
    """SDK can_use_tool callback.

    This is only invoked when a tool would otherwise ask for permission. Tools already
    allowed by settings/allowed_tools do not hit this callback; use hooks for full audit.
    """
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        matched = _is_dangerous_bash(command)
        if matched:
            return PermissionResultDeny(
                message=f"Blocked dangerous Bash command by policy: pattern={matched}",
                interrupt=True,
            )
    return PermissionResultAllow()


async def pre_tool_use_hook(input_data: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]) -> dict[str, Any]:
    """PreToolUse hook used for deterministic runtime enforcement."""
    tool_name = input_data.get("tool_name")
    tool_input = input_data.get("tool_input") or {}
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        matched = _is_dangerous_bash(command)
        if matched:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Blocked by container policy: {matched}",
                }
            }
    return {}


def build_profile_pre_tool_use_hook(profile: AgentRuntimeProfile) -> Any:
    async def profile_pre_tool_use_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input") or {}
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            return {}
        denial = _path_policy_denial(tool_name, tool_input, profile)
        if denial is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": denial,
            }
        }

    return profile_pre_tool_use_hook


def build_default_hooks(profile: AgentRuntimeProfile | None = None) -> dict[str, list[HookMatcher]]:
    pre_tool_use = [HookMatcher(matcher="Bash", hooks=[pre_tool_use_hook])]
    if profile is not None:
        profile_hook = build_profile_pre_tool_use_hook(profile)
        pre_tool_use.extend(HookMatcher(matcher=tool_name, hooks=[profile_hook]) for tool_name in PATH_TOOL_NAMES)
    return {"PreToolUse": pre_tool_use}
