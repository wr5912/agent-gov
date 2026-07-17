from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, TypedDict
from urllib.parse import urlparse

from app.runtime.business_agent_seed_catalog import runtime_volume_seeds_dir

SECURITY_OPERATIONS_EXPERT_AGENT_ID = "security-operations-expert"
BASH_ALLOW_RULE = "Bash(*)"
GENERIC_MCP_MUTATION_RULES = (
    "mcp__*__*write*",
    "mcp__*__*update*",
    "mcp__*__*delete*",
    "mcp__*__*block*",
    "mcp__*__*isolate*",
    "mcp__*__*disable*",
    "mcp__*__*kill*",
    "mcp__*__*quarantine*",
)

_SETTINGS_PATH = Path(".claude/settings.json")
_MCP_PATH = Path(".mcp.json")
_COMMON_HOOK_PATHS = (
    "hooks/pre_tool_guard.py",
    "hooks/post_tool_audit.py",
    "hooks/session_start.py",
)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_MCP_ENV_URL_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}(?:[/?#][^\s]*)?$")
_PROJECT_HOOK_RE = re.compile(
    r"(?:\$\{?CLAUDE_PROJECT_DIR\}?/|(?<![A-Za-z0-9_.-])\./)?"
    r"(?P<path>hooks/[A-Za-z0-9_./-]+)"
)


def managed_workspace_policy_paths(agent_id: str) -> tuple[str, ...]:
    del agent_id
    return (_SETTINGS_PATH.as_posix(), _MCP_PATH.as_posix(), *_COMMON_HOOK_PATHS)


class ManagedAgentPolicyError(RuntimeError):
    """Raised when a workspace cannot be executed safely by Claude Code."""


class RuntimeWorkspaceProfile(Protocol):
    @property
    def category(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def workspace_dir(self) -> Path: ...

    @property
    def data_dir(self) -> Path: ...

    @property
    def langfuse_observation_name(self) -> str: ...


class _PolicyProjectionEntry(TypedDict):
    agent_id: str
    compliant: bool
    violations: list[tuple[str, str]]


@dataclass(frozen=True)
class PolicyViolation:
    agent_id: str
    path: str
    rule_id: str
    detail: str


@dataclass(frozen=True)
class WorkspacePolicyPlan:
    agent_id: str
    workspace: Path
    violations: tuple[PolicyViolation, ...]

    @property
    def is_compliant(self) -> bool:
        return not self.violations


def _managed_relative_path(path: Path, anchor: Path) -> Path:
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise ValueError("managed path escapes its workspace") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("managed path is invalid")
    return relative


def _open_parent(anchor: Path, relative: Path) -> int:
    descriptor = os.open(anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        for part in relative.parent.parts:
            child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_regular_text(
    path: Path,
    *,
    workspace: Path,
    agent_id: str,
    required: bool,
) -> tuple[str | None, PolicyViolation | None]:
    try:
        relative = _managed_relative_path(path, workspace)
        parent_descriptor = _open_parent(workspace, relative)
    except FileNotFoundError:
        if not required:
            return None, None
        return None, PolicyViolation(agent_id, path.as_posix(), "referenced_hook_missing", "file is missing")
    except (OSError, ValueError) as exc:
        return None, PolicyViolation(agent_id, path.as_posix(), "unsafe_file_type", exc.__class__.__name__)
    try:
        try:
            descriptor = os.open(relative.name, _FILE_OPEN_FLAGS, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not required:
                return None, None
            return None, PolicyViolation(agent_id, path.as_posix(), "referenced_hook_missing", "file is missing")
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None, PolicyViolation(agent_id, path.as_posix(), "unsafe_file_type", "path is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
                return stream.read(), None
        finally:
            os.close(descriptor)
    except (OSError, UnicodeDecodeError) as exc:
        return None, PolicyViolation(agent_id, path.as_posix(), "workspace_file_unreadable", exc.__class__.__name__)
    finally:
        os.close(parent_descriptor)


def _valid_mcp_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if _MCP_ENV_URL_RE.fullmatch(value):
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_mcp_content(content: str, *, agent_id: str) -> tuple[PolicyViolation, ...]:
    path = _MCP_PATH.as_posix()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return (PolicyViolation(agent_id, path, "invalid_mcp_json", exc.__class__.__name__),)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return (PolicyViolation(agent_id, path, "invalid_mcp_servers", "mcpServers must be an object"),)

    violations: list[PolicyViolation] = []
    for name, config in servers.items():
        if not isinstance(config, dict):
            violations.append(PolicyViolation(agent_id, path, "invalid_mcp_server", str(name)))
            continue
        url = config.get("url")
        command = config.get("command")
        transport = str(config.get("type") or "").lower()
        if url is not None and not _valid_mcp_url(url):
            violations.append(PolicyViolation(agent_id, path, "invalid_mcp_url", str(name)))
        if transport in {"http", "sse"} and url is None:
            violations.append(PolicyViolation(agent_id, path, "missing_mcp_url", str(name)))
        if command is not None and (not isinstance(command, str) or not command.strip()):
            violations.append(PolicyViolation(agent_id, path, "invalid_mcp_command", str(name)))
    return tuple(violations)


def validate_managed_mcp_content(
    content: str,
    *,
    agent_id: str,
    runtime_mode: str,
    env: Mapping[str, str],
    runtime_root: Path,
    template_dir: Path | None = None,
) -> tuple[PolicyViolation, ...]:
    """Validate only Claude Code MCP syntax; seed content is not an authority."""

    del runtime_mode, env, runtime_root, template_dir
    return validate_mcp_content(content, agent_id=agent_id)


def _hook_paths(settings: object) -> tuple[PurePosixPath, ...]:
    if not isinstance(settings, dict):
        return ()
    hooks = settings.get("hooks")
    if hooks is None:
        return ()
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    paths: set[PurePosixPath] = set()
    for matchers in hooks.values():
        if not isinstance(matchers, list):
            raise ValueError("hook matchers must be a list")
        for matcher in matchers:
            if not isinstance(matcher, dict):
                raise ValueError("hook matcher must be an object")
            commands = matcher.get("hooks")
            if not isinstance(commands, list):
                raise ValueError("hook commands must be a list")
            for hook in commands:
                if not isinstance(hook, dict):
                    raise ValueError("hook command must be an object")
                command = hook.get("command")
                if command is None:
                    continue
                if not isinstance(command, str):
                    raise ValueError("hook command must be a string")
                for match in _PROJECT_HOOK_RE.finditer(command):
                    relative = PurePosixPath(match.group("path"))
                    if any(part in {"", ".", ".."} for part in relative.parts):
                        raise ValueError("hook path is invalid")
                    paths.add(relative)
    return tuple(sorted(paths, key=PurePosixPath.as_posix))


def referenced_workspace_hook_paths(settings_content: str) -> tuple[str, ...]:
    """Return project-relative hook files referenced by Claude Code settings."""

    settings = json.loads(settings_content)
    if not isinstance(settings, dict):
        raise ValueError("settings root must be an object")
    return tuple(path.as_posix() for path in _hook_paths(settings))


def plan_workspace_policy(*, workspace: Path, agent_id: str) -> WorkspacePolicyPlan:
    violations: list[PolicyViolation] = []

    settings_text, violation = _read_regular_text(
        workspace / _SETTINGS_PATH,
        workspace=workspace,
        agent_id=agent_id,
        required=False,
    )
    if violation:
        violations.append(violation)
    elif settings_text is not None:
        try:
            settings = json.loads(settings_text)
            if not isinstance(settings, dict):
                raise ValueError("settings root must be an object")
            hook_paths = _hook_paths(settings)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            violations.append(PolicyViolation(agent_id, _SETTINGS_PATH.as_posix(), "invalid_settings", exc.__class__.__name__))
        else:
            for relative in hook_paths:
                _, hook_violation = _read_regular_text(
                    workspace.joinpath(*relative.parts),
                    workspace=workspace,
                    agent_id=agent_id,
                    required=True,
                )
                if hook_violation:
                    violations.append(hook_violation)

    mcp_text, violation = _read_regular_text(
        workspace / _MCP_PATH,
        workspace=workspace,
        agent_id=agent_id,
        required=False,
    )
    if violation:
        violations.append(violation)
    elif mcp_text is not None:
        violations.extend(validate_mcp_content(mcp_text, agent_id=agent_id))

    return WorkspacePolicyPlan(agent_id=agent_id, workspace=workspace, violations=tuple(violations))


def policy_projection(plans: Iterable[WorkspacePolicyPlan]) -> str:
    projection: list[_PolicyProjectionEntry] = []
    for plan in sorted(plans, key=lambda item: item.agent_id):
        projection.append(
            {
                "agent_id": plan.agent_id,
                "compliant": plan.is_compliant,
                "violations": [(item.path, item.rule_id) for item in plan.violations],
            }
        )
    return hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def raise_for_policy_violations(violations: Iterable[PolicyViolation]) -> None:
    items = list(violations)
    if not items:
        return
    summary = "; ".join(f"{item.agent_id}:{item.path}:{item.rule_id}" for item in items)
    raise ManagedAgentPolicyError(summary)


def runtime_workspace_policy_violations(
    *,
    workspace: Path,
    agent_id: str,
    runtime_mode: str,
    env: Mapping[str, str],
    runtime_root: Path,
    template_dir: Path | None = None,
) -> tuple[PolicyViolation, ...]:
    del runtime_mode, env, runtime_root, template_dir
    return plan_workspace_policy(workspace=workspace, agent_id=agent_id).violations


def require_runtime_workspace_policy(
    *,
    workspace: Path,
    agent_id: str,
    runtime_mode: str,
    env: Mapping[str, str],
    runtime_root: Path,
    template_dir: Path | None = None,
) -> None:
    raise_for_policy_violations(
        runtime_workspace_policy_violations(
            workspace=workspace,
            agent_id=agent_id,
            runtime_mode=runtime_mode,
            env=env,
            runtime_root=runtime_root,
            template_dir=template_dir,
        )
    )


def require_profile_runtime_workspace_policy(
    profile: RuntimeWorkspaceProfile,
    *,
    runtime_mode: str,
    env: Mapping[str, str],
) -> None:
    if profile.category != "business":
        return
    runtime_root = Path("/") if profile.data_dir.resolve() == Path("/data") else profile.data_dir.resolve().parent
    candidate_prefix = "runtime.candidate."
    agent_id = (
        profile.langfuse_observation_name.removeprefix(candidate_prefix) if profile.langfuse_observation_name.startswith(candidate_prefix) else profile.name
    )
    require_runtime_workspace_policy(
        workspace=profile.workspace_dir,
        agent_id=agent_id,
        runtime_mode=runtime_mode,
        env=env,
        runtime_root=runtime_root,
    )


def default_runtime_template_dir() -> Path:
    return runtime_volume_seeds_dir()
