"""AgentScope Harness 的 fail-closed 静态安全策略。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict
from urllib.parse import urlsplit

import yaml
from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT
from agentgov_subagent_manifest_policy import validate_subagent_manifest

_MANIFEST_PATH = Path("agent.yaml")
_PROMPT_PATH = Path("AGENT.md")
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}")
_MCP_RESOURCE_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_MCP_TEMPLATE_VARIABLE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_FORBIDDEN_MCP_HEADERS = {
    "connection",
    "content-length",
    "forwarded",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "via",
    "x-original-url",
    "x-rewrite-url",
}
_RESERVED_RESOURCE_TOOL_NAMES = {
    "resources_list",
    "resource_templates_list",
    "resource_read",
}
_PERMISSION_MODES = {"default", "explore", "accept_edits", "dont_ask"}
_TOOL_POLICY_FIELDS = ("allowed_tools", "ask_tools", "denied_tools")


def _mcp_env_prefix(server_name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", server_name.upper()).strip("_")
    return f"{normalized}_MCP_"


def _forbidden_mcp_header(name: str) -> bool:
    normalized = name.casefold()
    return normalized in _FORBIDDEN_MCP_HEADERS or normalized.startswith("x-forwarded-")


def managed_workspace_policy_paths(agent_id: str) -> tuple[str, ...]:
    del agent_id
    return (_MANIFEST_PATH.as_posix(), _PROMPT_PATH.as_posix())


class ManagedAgentPolicyError(RuntimeError):
    """Harness 无法被 AgentScope Runtime 安全执行。"""


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


def _read_regular_text(path: Path, *, workspace: Path, agent_id: str, required: bool) -> tuple[str | None, PolicyViolation | None]:
    try:
        relative = path.relative_to(workspace)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("invalid managed path")
        parent_fd = os.open(workspace, _DIRECTORY_OPEN_FLAGS)
        try:
            for part in relative.parent.parts:
                child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child
            try:
                descriptor = os.open(relative.name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if not required:
                    return None, None
                return None, PolicyViolation(agent_id, relative.as_posix(), "required_asset_missing", "file is missing")
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    return None, PolicyViolation(agent_id, relative.as_posix(), "unsafe_file_type", "not a regular file")
                with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
                    return stream.read(), None
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
    except FileNotFoundError:
        if not required:
            return None, None
        return None, PolicyViolation(agent_id, path.name, "required_asset_missing", "file is missing")
    except (OSError, UnicodeError, ValueError) as exc:
        return None, PolicyViolation(agent_id, path.name, "workspace_file_unreadable", exc.__class__.__name__)


def validate_mcp_content(content: str, *, agent_id: str, path: str = "mcp") -> tuple[PolicyViolation, ...]:
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError as exc:
        return (PolicyViolation(agent_id, path, "invalid_mcp_json", str(exc)),)
    if not isinstance(loaded, dict):
        return (PolicyViolation(agent_id, path, "invalid_mcp_config", "root must be an object"),)
    if loaded.get("schema_version") != 1 or not isinstance(loaded.get("name"), str) or _MCP_TOOL_NAME.fullmatch(str(loaded.get("name"))) is None:
        return (PolicyViolation(agent_id, path, "invalid_mcp_config", "schema_version=1 and name are required"),)
    capability_violation = _mcp_capability_violation(loaded, agent_id=agent_id, path=path)
    if capability_violation is not None:
        return (capability_violation,)
    references = loaded.get("credential_refs")
    if not isinstance(references, list):
        return (PolicyViolation(agent_id, path, "credential_refs_missing", "credential_refs must be a list"),)
    reference_envs: set[str] = set()
    reference_paths: set[str] = set()
    server_name = str(loaded["name"])
    env_prefix = _mcp_env_prefix(server_name)
    for reference in references:
        if not isinstance(reference, dict):
            return (PolicyViolation(agent_id, path, "invalid_credential_ref", "credential reference must be an object"),)
        env_name = reference.get("env")
        target = reference.get("path")
        if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            return (PolicyViolation(agent_id, path, "invalid_credential_ref", "credential env name is invalid"),)
        if not env_name.startswith(env_prefix):
            return (PolicyViolation(agent_id, path, "invalid_credential_ref", "credential env must be scoped to its MCP server"),)
        if not isinstance(target, str) or not target.startswith("mcp_config."):
            return (PolicyViolation(agent_id, path, "invalid_credential_ref", "credential path must target mcp_config"),)
        reference_envs.add(env_name)
        reference_paths.add(target)
    config = loaded.get("mcp_config")
    if not isinstance(config, dict) or config.get("type") != "http_mcp":
        return (PolicyViolation(agent_id, path, "invalid_mcp_config", "only http_mcp is allowed"),)
    url = config.get("url")
    if not isinstance(url, str) or not _valid_mcp_http_url(url):
        return (PolicyViolation(agent_id, path, "invalid_mcp_url", "http_mcp.url must be HTTP(S) or a credential placeholder"),)
    if url != "${" + env_prefix + "URL}":
        return (
            PolicyViolation(
                agent_id,
                path,
                "runtime_bound_mcp_endpoint_required",
                "http_mcp.url must use a declared Runtime environment reference",
            ),
        )
    if "mcp_config.url" not in reference_paths:
        return (PolicyViolation(agent_id, path, "credential_ref_missing", "mcp_config.url is not declared"),)
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        return (PolicyViolation(agent_id, path, "inline_mcp_secret", "URL userinfo is forbidden"),)
    if url.split("?", 1)[0].endswith(("/sse", "/messages/")):
        return (PolicyViolation(agent_id, path, "invalid_mcp_url", "streamable HTTP /mcp transport is required"),)
    headers = config.get("headers", {})
    if not isinstance(headers, dict):
        return (PolicyViolation(agent_id, path, "invalid_mcp_config", "headers must be an object"),)
    seen_headers: set[str] = set()
    for header_name, header_value in headers.items():
        normalized_header = header_name.casefold()
        if _HTTP_HEADER_NAME.fullmatch(header_name) is None or _forbidden_mcp_header(header_name) or normalized_header in seen_headers:
            return (PolicyViolation(agent_id, path, "invalid_mcp_header", "header name is invalid, duplicated, or forbidden"),)
        seen_headers.add(normalized_header)
        target = f"mcp_config.headers.{header_name}"
        if not isinstance(header_value, str):
            return (PolicyViolation(agent_id, path, "invalid_mcp_config", "header values must be strings"),)
        if not _placeholder_envs(header_value):
            return (PolicyViolation(agent_id, path, "inline_mcp_secret", f"{target} must use a Runtime environment reference"),)
        if target not in reference_paths:
            return (PolicyViolation(agent_id, path, "credential_ref_missing", f"{target} is not declared"),)
    undeclared = _placeholder_envs(json.dumps(config, ensure_ascii=False)) - reference_envs
    if undeclared:
        return (PolicyViolation(agent_id, path, "credential_ref_missing", f"undeclared credential placeholders: {sorted(undeclared)}"),)
    return ()


def _mcp_capability_violation(
    loaded: Mapping[str, object],
    *,
    agent_id: str,
    path: str,
) -> PolicyViolation | None:
    for field in ("enable_tools", "enable_resources", "enable_resource_templates"):
        value = loaded.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value) or len(value) != len(set(value)):
            return PolicyViolation(
                agent_id,
                path,
                "invalid_mcp_allowlist",
                f"{field} must be an explicit unique string list",
            )
    tools = loaded["enable_tools"]
    assert isinstance(tools, list)
    if any(_MCP_TOOL_NAME.fullmatch(tool) is None or any(character in tool for character in "*?[") or tool in _RESERVED_RESOURCE_TOOL_NAMES for tool in tools):
        return PolicyViolation(agent_id, path, "invalid_mcp_allowlist", "enable_tools must contain exact tool names")
    resources = loaded["enable_resources"]
    templates = loaded["enable_resource_templates"]
    assert isinstance(resources, list) and isinstance(templates, list)
    if any(_MCP_RESOURCE_URI.fullmatch(uri) is None or "{" in uri or "}" in uri for uri in resources):
        return PolicyViolation(agent_id, path, "invalid_mcp_allowlist", "enable_resources contains an invalid URI")
    for template in templates:
        probe = _MCP_TEMPLATE_VARIABLE.sub("agentgov-probe", template)
        if _MCP_RESOURCE_URI.fullmatch(probe) is None or probe == template or "{" in probe or "}" in probe:
            return PolicyViolation(
                agent_id,
                path,
                "invalid_mcp_allowlist",
                "enable_resource_templates must use simple named variables",
            )
    if loaded.get("disable_tools") not in (None, []):
        return PolicyViolation(agent_id, path, "invalid_mcp_allowlist", "disable_tools is forbidden")
    return None


def _placeholder_envs(value: str) -> set[str]:
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?::-[^}]*)?\}", value))


def _valid_mcp_http_url(value: str) -> bool:
    if re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*(?::-(?:https?://[^}]+))?\}", value):
        return True
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def validate_managed_mcp_content(
    content: str,
    *,
    agent_id: str,
    runtime_mode: str,
    env: Mapping[str, str],
    runtime_root: Path,
    bootstrap_dir: Path | None = None,
) -> tuple[PolicyViolation, ...]:
    del runtime_mode, env, runtime_root, bootstrap_dir
    return validate_mcp_content(content, agent_id=agent_id)


def referenced_workspace_hook_paths(settings_content: str) -> tuple[str, ...]:
    """旧 hook 不属于新 Harness；保留空结果以兼容版本校验调用点。"""

    del settings_content
    return ()


def plan_workspace_policy(*, workspace: Path, agent_id: str) -> WorkspacePolicyPlan:
    violations: list[PolicyViolation] = []
    manifest_text, violation = _read_regular_text(workspace / _MANIFEST_PATH, workspace=workspace, agent_id=agent_id, required=True)
    if violation:
        violations.append(violation)
    elif manifest_text is not None:
        try:
            manifest = yaml.safe_load(manifest_text) or {}
        except yaml.YAMLError as exc:
            violations.append(PolicyViolation(agent_id, _MANIFEST_PATH.as_posix(), "invalid_manifest", str(exc)))
        else:
            violations.extend(_manifest_violations(manifest, agent_id=agent_id))
    _, violation = _read_regular_text(workspace / _PROMPT_PATH, workspace=workspace, agent_id=agent_id, required=True)
    if violation:
        violations.append(violation)
    mcp_root = workspace / "mcp"
    if mcp_root.exists():
        if mcp_root.is_symlink() or not mcp_root.is_dir():
            violations.append(PolicyViolation(agent_id, "mcp", "unsafe_file_type", "mcp must be a regular directory"))
        else:
            for path in sorted(mcp_root.glob("*.json")):
                text, item_violation = _read_regular_text(path, workspace=workspace, agent_id=agent_id, required=True)
                if item_violation:
                    violations.append(item_violation)
                elif text is not None:
                    violations.extend(validate_mcp_content(text, agent_id=agent_id, path=path.relative_to(workspace).as_posix()))
    violations.extend(_subagent_policy_violations(workspace, agent_id=agent_id))
    return WorkspacePolicyPlan(agent_id=agent_id, workspace=workspace, violations=tuple(violations))


def _subagent_policy_violations(workspace: Path, *, agent_id: str) -> tuple[PolicyViolation, ...]:
    root = workspace / "subagents"
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        return (PolicyViolation(agent_id, "subagents", "unsafe_file_type", "subagents must be a regular directory"),)
    failures: list[PolicyViolation] = []
    for entry in sorted(root.iterdir()):
        relative = entry.relative_to(workspace).as_posix()
        if entry.is_symlink() or not entry.is_dir():
            failures.append(PolicyViolation(agent_id, relative, "unsafe_file_type", "subagent must be a regular directory"))
            continue
        text, violation = _read_regular_text(entry / _MANIFEST_PATH, workspace=workspace, agent_id=agent_id, required=True)
        if violation is not None:
            failures.append(violation)
            continue
        try:
            manifest = yaml.safe_load(text or "")
        except yaml.YAMLError as exc:
            failures.append(PolicyViolation(agent_id, f"{relative}/agent.yaml", "invalid_manifest", str(exc)))
            continue
        _, prompt_violation = _read_regular_text(
            entry / _PROMPT_PATH,
            workspace=workspace,
            agent_id=agent_id,
            required=True,
        )
        if prompt_violation is not None:
            failures.append(prompt_violation)
        failures.extend(
            PolicyViolation(
                agent_id,
                f"{relative}/agent.yaml",
                issue.code,
                issue.detail,
            )
            for issue in validate_subagent_manifest(
                manifest,
                directory_name=entry.name,
            )
        )
    return tuple(failures)


def _manifest_violations(value: object, *, agent_id: str) -> tuple[PolicyViolation, ...]:
    path = _MANIFEST_PATH.as_posix()
    if not isinstance(value, dict):
        return (PolicyViolation(agent_id, path, "invalid_manifest", "root must be an object"),)
    agent = value.get("agent")
    session = value.get("session", {})
    policy = value.get("workspace_policy")
    failures: list[PolicyViolation] = []
    if value.get("schema_version") != 1:
        failures.append(PolicyViolation(agent_id, path, "invalid_schema_version", "schema_version must be 1"))
    if not isinstance(agent, dict) or agent.get("runtime") != "agentscope" or agent.get("runtime_contract") != AGENTSCOPE_RUNTIME_CONTRACT:
        failures.append(PolicyViolation(agent_id, path, "invalid_runtime_contract", "AgentScope 2.0.8 contract is required"))
    if not isinstance(policy, dict) or policy.get("fail_closed") is not True or policy.get("immutable_harness") is not True:
        failures.append(PolicyViolation(agent_id, path, "unsafe_workspace_policy", "fail_closed and immutable_harness are required"))
    if isinstance(policy, dict) and policy.get("allow_for_run") is not False:
        failures.append(PolicyViolation(agent_id, path, "persistent_permission_forbidden", "allow_for_run must be false"))
    if not isinstance(session, dict) or session.get("permission_mode", "default") not in _PERMISSION_MODES:
        failures.append(PolicyViolation(agent_id, path, "invalid_permission_mode", "permission_mode is invalid or unsafe"))
    if isinstance(policy, dict):
        failures.extend(_tool_policy_violations(policy, agent_id=agent_id, path=path))
    return tuple(failures)


def _tool_policy_violations(
    policy: Mapping[str, object],
    *,
    agent_id: str,
    path: str,
) -> tuple[PolicyViolation, ...]:
    parsed: dict[str, set[tuple[str, str | None]]] = {}
    failures: list[PolicyViolation] = []
    for field in _TOOL_POLICY_FIELDS:
        # 历史不可变 Harness 可能尚未写出权限列表；受管发布校验保持既有
        # 结构兼容，Runtime 加载时仍会对其必需 allow/deny 字段 fail-closed。
        value = policy.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) or item != item.strip() or not item or "\0" in item for item in value):
            failures.append(PolicyViolation(agent_id, path, "invalid_tool_policy", f"{field} must be a string list"))
            parsed[field] = set()
            continue
        rules: list[tuple[str, str | None]] = []
        invalid = False
        for item in value:
            name, separator, content = item.partition("(")
            if not name or (separator and (not item.endswith(")") or not content[:-1])):
                invalid = True
                break
            if field in {"allowed_tools", "ask_tools"} and name.startswith("mcp__") and any(character in name for character in "*?["):
                failures.append(
                    PolicyViolation(
                        agent_id,
                        path,
                        "wildcard_mcp_permission_forbidden",
                        f"{field} cannot wildcard MCP tools",
                    ),
                )
                invalid = True
                break
            rules.append((name, content[:-1] if separator else None))
        if invalid or len(rules) != len(set(rules)):
            failures.append(PolicyViolation(agent_id, path, "invalid_tool_policy", f"{field} contains invalid or duplicate rules"))
        parsed[field] = set(rules)
    for index, left in enumerate(_TOOL_POLICY_FIELDS):
        for right in _TOOL_POLICY_FIELDS[index + 1 :]:
            if parsed[left] & parsed[right]:
                failures.append(
                    PolicyViolation(
                        agent_id,
                        path,
                        "conflicting_tool_policy",
                        f"{left} and {right} contain the same rule",
                    ),
                )
    return tuple(failures)


def policy_projection(plans: Iterable[WorkspacePolicyPlan]) -> str:
    projection: list[_PolicyProjectionEntry] = [
        {
            "agent_id": plan.agent_id,
            "compliant": plan.is_compliant,
            "violations": [(item.path, item.rule_id) for item in plan.violations],
        }
        for plan in sorted(plans, key=lambda item: item.agent_id)
    ]
    return hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def raise_for_policy_violations(violations: Iterable[PolicyViolation]) -> None:
    items = list(violations)
    if items:
        raise ManagedAgentPolicyError("; ".join(f"{item.agent_id}:{item.path}:{item.rule_id}" for item in items))


def runtime_workspace_policy_violations(
    *,
    workspace: Path,
    agent_id: str,
    runtime_mode: str,
    env: Mapping[str, str],
    runtime_root: Path,
    bootstrap_dir: Path | None = None,
) -> tuple[PolicyViolation, ...]:
    del runtime_mode, env, runtime_root, bootstrap_dir
    return plan_workspace_policy(workspace=workspace, agent_id=agent_id).violations


def require_runtime_workspace_policy(
    *,
    workspace: Path,
    agent_id: str,
    runtime_mode: str,
    env: Mapping[str, str],
    runtime_root: Path,
    bootstrap_dir: Path | None = None,
) -> None:
    raise_for_policy_violations(
        runtime_workspace_policy_violations(
            workspace=workspace,
            agent_id=agent_id,
            runtime_mode=runtime_mode,
            env=env,
            runtime_root=runtime_root,
            bootstrap_dir=bootstrap_dir,
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
    require_runtime_workspace_policy(
        workspace=profile.workspace_dir,
        agent_id=profile.name,
        runtime_mode=runtime_mode,
        env=env,
        runtime_root=profile.data_dir.resolve().parent,
    )
