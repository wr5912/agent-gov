from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict
from urllib.parse import urlparse

import yaml
from scripts.runtime_template_renderer import RuntimeTemplateRenderContext, build_render_context, render_template_file

SECURITY_OPERATIONS_EXPERT_AGENT_ID = "security-operations-expert"
BASH_ALLOW_RULE = "Bash(*)"
_MANAGED_SETTINGS_PATH = ".claude/settings.json"
_MANAGED_MCP_PATH = ".mcp.json"
_MANAGED_PRE_TOOL_GUARD_PATH = "hooks/pre_tool_guard.py"
_BASE_MANAGED_POLICY_PATHS = (
    _MANAGED_SETTINGS_PATH,
    _MANAGED_MCP_PATH,
    _MANAGED_PRE_TOOL_GUARD_PATH,
)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
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
AGENT_MCP_MUTATION_RULES = {
    "response-disposal": (
        "mcp__soc-playbook-execution__*",
        "mcp__soc-playbook-registry__*",
    )
}
SECURITY_PROTECTED_ASK_RULES = (
    "mcp__sec-ops__soc_api__create",
    "mcp__sec-ops__soc_api__manual",
)
SECURITY_DIRECT_ALLOW_RULES = (
    BASH_ALLOW_RULE,
    "Edit(./**)",
    "Write(./**)",
    "mcp__sec-ops__*",
)
SECURITY_FORBIDDEN_RULES = (
    "mcp__sec-ops__soc_api__execute",
    "mcp__sec-ops__soc_api__create_1",
    "mcp__sec-ops__soc_api__update*",
    "mcp__sec-ops__soc_api__delete*",
    "mcp__sec-ops__soc_api__upload*",
    "mcp__sec-ops__soc_api__cancel*",
    "mcp__sec-ops__soc_api__rollback*",
)
SECURITY_LEGACY_MUTATION_RULES = (
    "mcp__sec-ops__*_post",
    "mcp__sec-ops__*_put",
    "mcp__sec-ops__*_patch",
    "mcp__sec-ops__*execute*",
    "mcp__sec-ops__*manual*",
    "mcp__sec-ops__*create*",
    "mcp__sec-ops__*update*",
    "mcp__sec-ops__*delete*",
    "mcp__sec-ops__*write*",
    "mcp__sec-ops__*upload*",
    "mcp__sec-ops__*cancel*",
    "mcp__sec-ops__*block*",
    "mcp__sec-ops__*isolate*",
    "mcp__sec-ops__*disable*",
    "mcp__sec-ops__*kill*",
    "mcp__sec-ops__*quarantine*",
)

_SECURITY_MANAGED_TEXT_FILES = {
    "CLAUDE.md": (
        ("<!-- AGENTGOV:SECURITY-RESPONSE:START -->", "<!-- AGENTGOV:SECURITY-RESPONSE:END -->"),
        ("<!-- AGENTGOV:SECURITY-HITL:START -->", "<!-- AGENTGOV:SECURITY-HITL:END -->"),
    ),
    "agent.yaml": (("# AGENTGOV:SECURITY-APPROVAL:START", "# AGENTGOV:SECURITY-APPROVAL:END"),),
    ".claude/skills/threat-response-disposition/SKILL.md": (
        ("# AGENTGOV:THREAT-SKILL:START", "# AGENTGOV:THREAT-SKILL:END"),
        ("<!-- AGENTGOV:THREAT-SKILL-BODY:START -->", "<!-- AGENTGOV:THREAT-SKILL-BODY:END -->"),
    ),
}
_SECURITY_LEGACY_FILE_HASHES = {
    "CLAUDE.md": {
        "c44269494e2e40dff8b895705681d9d85216b67987ba74c3136e82737688a067",
        "2f6528df5e8c004124db35ddaf431480710d8b590a835f4efa645b00010c549b",
    },
    "agent.yaml": {
        "02818bd8d03dd042d8925fac06739a4ff48adb80ddbbafcc2fe383340d710109",
        "e435659e80258e57d0767cfe4458fde52841470799eb4b3a9a1452fdd5a39713",
    },
    ".claude/skills/threat-response-disposition/SKILL.md": {
        "07b4d5fcf907d8c294b30996ca26718b20294d79984e1c1b983bf0ce7c85bfc0",
        "c74c03b643e7010eb105a716f1f84545570b17dec90ab3c88fc39e29c4199076",
    },
}


def managed_workspace_policy_paths(agent_id: str) -> tuple[str, ...]:
    paths = _BASE_MANAGED_POLICY_PATHS
    if agent_id == SECURITY_OPERATIONS_EXPERT_AGENT_ID:
        paths += tuple(_SECURITY_MANAGED_TEXT_FILES)
    return paths


class ManagedAgentPolicyError(RuntimeError):
    """Raised when managed policy cannot be safely validated or migrated."""


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
    changes: list[tuple[str, str, str]]
    violations: list[tuple[str, str]]


@dataclass(frozen=True)
class PolicyViolation:
    agent_id: str
    path: str
    rule_id: str
    detail: str


@dataclass(frozen=True)
class PolicyChange:
    agent_id: str
    path: Path
    rule_id: str
    before_sha256: str | None
    after_sha256: str
    content: str
    mode: int | None = None


@dataclass(frozen=True)
class WorkspacePolicyPlan:
    agent_id: str
    workspace: Path
    changes: tuple[PolicyChange, ...]
    violations: tuple[PolicyViolation, ...]

    @property
    def is_compliant(self) -> bool:
        return not self.changes and not self.violations


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _managed_relative_path(path: Path, anchor: Path) -> Path:
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise ValueError("managed path escapes its anchor") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("managed path is invalid")
    return relative


def _open_managed_parent(anchor: Path, relative: Path) -> int:
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
    anchor: Path,
    agent_id: str,
    required: bool = True,
) -> tuple[str | None, PolicyViolation | None]:
    try:
        relative = _managed_relative_path(path, anchor)
        parent_descriptor = _open_managed_parent(anchor, relative)
    except FileNotFoundError:
        if not required:
            return None, None
        return None, PolicyViolation(agent_id, path.as_posix(), "required_file_missing", "required managed file is missing")
    except (OSError, ValueError) as exc:
        return None, PolicyViolation(agent_id, path.as_posix(), "unsafe_file_type", exc.__class__.__name__)
    try:
        try:
            descriptor = os.open(relative.name, _FILE_OPEN_FLAGS, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not required:
                return None, None
            return None, PolicyViolation(agent_id, path.as_posix(), "required_file_missing", "required managed file is missing")
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None, PolicyViolation(agent_id, path.as_posix(), "unsafe_file_type", "managed path is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
                return stream.read(), None
        finally:
            os.close(descriptor)
    except (OSError, UnicodeDecodeError) as exc:
        rule_id = "unsafe_file_type" if isinstance(exc, OSError) else "managed_file_unreadable"
        return None, PolicyViolation(agent_id, path.as_posix(), rule_id, exc.__class__.__name__)
    finally:
        os.close(parent_descriptor)


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _append_once(values: list[str], additions: Iterable[str]) -> list[str]:
    for item in additions:
        if item not in values:
            values.append(item)
    return values


def _reconciled_settings(text: str, *, agent_id: str, mode: str) -> str:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("settings root must be an object")
    permissions = data.setdefault("permissions", {})
    sandbox = data.setdefault("sandbox", {})
    if not isinstance(permissions, dict) or not isinstance(sandbox, dict):
        raise ValueError("settings permissions/sandbox must be objects")
    allow = _string_list(permissions.get("allow"))
    ask = _string_list(permissions.get("ask"))
    deny = _string_list(permissions.get("deny"))
    sandbox.update(
        {
            "enabled": True,
            "failIfUnavailable": True,
            "enableWeakerNestedSandbox": mode == "container",
            "allowUnsandboxedCommands": False,
        }
    )
    if agent_id == SECURITY_OPERATIONS_EXPERT_AGENT_ID:
        sandbox["autoAllowBashIfSandboxed"] = True
        blocked = set(SECURITY_PROTECTED_ASK_RULES + SECURITY_FORBIDDEN_RULES + SECURITY_LEGACY_MUTATION_RULES)
        allow = [item for item in allow if item not in blocked]
        ask = [item for item in ask if item not in blocked]
        deny = [item for item in deny if item not in SECURITY_PROTECTED_ASK_RULES]
        _append_once(allow, SECURITY_DIRECT_ALLOW_RULES)
        ask = list(SECURITY_PROTECTED_ASK_RULES) + ask
        _append_once(deny, ("AskUserQuestion", *SECURITY_FORBIDDEN_RULES))
    else:
        sandbox["autoAllowBashIfSandboxed"] = False
        mutation_rules = AGENT_MCP_MUTATION_RULES.get(agent_id, GENERIC_MCP_MUTATION_RULES)
        allow = [item for item in allow if item != BASH_ALLOW_RULE and item not in mutation_rules]
        ask = [item for item in ask if item != BASH_ALLOW_RULE]
        ask.insert(0, BASH_ALLOW_RULE)
        _append_once(ask, mutation_rules)
    permissions["allow"] = allow
    permissions["ask"] = ask
    permissions["deny"] = deny
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _valid_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def _reconciled_mcp(current_text: str, template_text: str) -> str:
    current = json.loads(current_text)
    desired = json.loads(template_text)
    if not isinstance(current, dict) or not isinstance(desired, dict):
        raise ValueError("MCP config root must be an object")
    current_servers = current.setdefault("mcpServers", {})
    desired_servers = desired.get("mcpServers", {})
    if not isinstance(current_servers, dict) or not isinstance(desired_servers, dict):
        raise ValueError("mcpServers must be an object")
    for name, desired_config in desired_servers.items():
        if not isinstance(desired_config, dict):
            raise ValueError(f"managed MCP server {name!r} must be an object")
        if desired_config.get("type") != "http" or not _valid_http_url(desired_config.get("url")):
            raise ValueError(f"managed MCP server {name!r} requires an HTTP(S) URL without embedded credentials")
        current_config = current_servers.get(name)
        if not isinstance(current_config, dict):
            current_config = {}
            current_servers[name] = current_config
        current_config["type"] = "http"
        current_config["url"] = desired_config["url"]
        current_config.pop("command", None)
        current_config.pop("args", None)
    return json.dumps(current, ensure_ascii=False, indent=2) + "\n"


def _replace_managed_block(current: str, template: str, start: str, end: str) -> str:
    current_start = current.find(start)
    template_start = template.find(start)
    if current_start < 0 or template_start < 0:
        raise ValueError(f"missing managed marker {start}")
    current_end = current.find(end, current_start + len(start))
    template_end = template.find(end, template_start + len(start))
    if current_end < 0 or template_end < 0:
        raise ValueError(f"missing managed marker {end}")
    current_end += len(end)
    template_end += len(end)
    return current[:current_start] + template[template_start:template_end] + current[current_end:]


def _reconciled_security_text(path: str, current: str, template: str) -> str:
    if current == template:
        return current
    if _sha256(current) in _SECURITY_LEGACY_FILE_HASHES[path]:
        return template
    result = current
    for start, end in _SECURITY_MANAGED_TEXT_FILES[path]:
        result = _replace_managed_block(result, template, start, end)
    return result


def _append_change(
    changes: list[PolicyChange],
    *,
    agent_id: str,
    path: Path,
    rule_id: str,
    before: str | None,
    after: str,
    mode: int | None = None,
) -> None:
    if before == after:
        return
    changes.append(PolicyChange(agent_id, path, rule_id, _sha256(before) if before is not None else None, _sha256(after), after, mode))


def _append_json_change(
    changes: list[PolicyChange],
    *,
    agent_id: str,
    path: Path,
    rule_id: str,
    before: str,
    after: str,
) -> None:
    if json.loads(before) == json.loads(after):
        return
    _append_change(
        changes,
        agent_id=agent_id,
        path=path,
        rule_id=rule_id,
        before=before,
        after=after,
    )


def plan_workspace_policy(
    *,
    workspace: Path,
    agent_id: str,
    template_workspace: Path | None,
    render_context: RuntimeTemplateRenderContext,
) -> WorkspacePolicyPlan:
    changes: list[PolicyChange] = []
    violations: list[PolicyViolation] = []
    settings_path = workspace / _MANAGED_SETTINGS_PATH
    settings_text, violation = _read_regular_text(settings_path, anchor=workspace, agent_id=agent_id)
    if violation:
        violations.append(violation)
    elif settings_text is not None:
        try:
            after = _reconciled_settings(settings_text, agent_id=agent_id, mode=render_context.mode)
            _append_json_change(
                changes,
                agent_id=agent_id,
                path=settings_path,
                rule_id="managed_settings",
                before=settings_text,
                after=after,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            violations.append(PolicyViolation(agent_id, settings_path.as_posix(), "invalid_settings", exc.__class__.__name__))

    mcp_path = workspace / _MANAGED_MCP_PATH
    mcp_text, violation = _read_regular_text(mcp_path, anchor=workspace, agent_id=agent_id)
    if violation:
        violations.append(violation)
    elif mcp_text is not None:
        try:
            after = mcp_text
            if template_workspace is not None:
                template_path = template_workspace / _MANAGED_MCP_PATH
                template_mcp, template_violation = _read_regular_text(
                    template_path,
                    anchor=template_workspace,
                    agent_id=agent_id,
                    required=False,
                )
                if template_violation:
                    violations.append(PolicyViolation(agent_id, mcp_path.as_posix(), "invalid_mcp_template", template_violation.rule_id))
                elif template_mcp is not None:
                    template_text = render_template_file(
                        template_mcp,
                        rel_path=Path(_MANAGED_MCP_PATH),
                        context=render_context,
                    )
                    after = _reconciled_mcp(mcp_text, template_text)
            _append_json_change(
                changes,
                agent_id=agent_id,
                path=mcp_path,
                rule_id="managed_mcp_servers",
                before=mcp_text,
                after=after,
            )
            violations.extend(validate_mcp_content(after, agent_id=agent_id))
        except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            violations.append(PolicyViolation(agent_id, mcp_path.as_posix(), "invalid_mcp_config", exc.__class__.__name__))

    if template_workspace is not None:
        _plan_pre_tool_guard(workspace, template_workspace, agent_id, changes, violations)
    if agent_id == SECURITY_OPERATIONS_EXPERT_AGENT_ID and template_workspace is not None:
        _plan_security_text_files(workspace, template_workspace, agent_id, changes, violations)
    return WorkspacePolicyPlan(agent_id, workspace, tuple(changes), tuple(violations))


def _plan_pre_tool_guard(
    workspace: Path,
    template_workspace: Path,
    agent_id: str,
    changes: list[PolicyChange],
    violations: list[PolicyViolation],
) -> None:
    relative = Path(_MANAGED_PRE_TOOL_GUARD_PATH)
    path = workspace / relative
    current, violation = _read_regular_text(path, anchor=workspace, agent_id=agent_id, required=False)
    if violation:
        violations.append(violation)
        return
    template_path = template_workspace / relative
    template, template_violation = _read_regular_text(template_path, anchor=template_workspace, agent_id=agent_id)
    if template_violation or template is None:
        detail = template_violation.rule_id if template_violation else "required_file_missing"
        violations.append(PolicyViolation(agent_id, path.as_posix(), "invalid_pre_tool_guard_template", detail))
        return
    try:
        _append_change(
            changes,
            agent_id=agent_id,
            path=path,
            rule_id="managed_pre_tool_guard",
            before=current,
            after=template,
            mode=0o600,
        )
    except OSError as exc:
        violations.append(PolicyViolation(agent_id, path.as_posix(), "invalid_pre_tool_guard_template", exc.__class__.__name__))


def _plan_security_text_files(
    workspace: Path,
    template_workspace: Path,
    agent_id: str,
    changes: list[PolicyChange],
    violations: list[PolicyViolation],
) -> None:
    for relative in _SECURITY_MANAGED_TEXT_FILES:
        path = workspace / relative
        template_path = template_workspace / relative
        current, violation = _read_regular_text(path, anchor=workspace, agent_id=agent_id)
        if violation:
            violations.append(violation)
            continue
        template, template_violation = _read_regular_text(template_path, anchor=template_workspace, agent_id=agent_id)
        if template_violation or template is None:
            detail = template_violation.rule_id if template_violation else "required_file_missing"
            violations.append(PolicyViolation(agent_id, path.as_posix(), "unknown_security_contract", detail))
            continue
        try:
            assert current is not None
            after = _reconciled_security_text(relative, current, template)
            _append_change(changes, agent_id=agent_id, path=path, rule_id="security_response_contract", before=current, after=after)
            if relative == "agent.yaml":
                violations.extend(validate_security_agent_yaml(after, path=path.as_posix()))
        except (AssertionError, OSError, UnicodeDecodeError, ValueError) as exc:
            violations.append(PolicyViolation(agent_id, path.as_posix(), "unknown_security_contract", str(exc)))


def validate_mcp_content(content: str, *, agent_id: str) -> tuple[PolicyViolation, ...]:
    path = ".mcp.json"
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
        if config.get("command") or str(config.get("type") or "").lower() == "stdio":
            violations.append(PolicyViolation(agent_id, path, "stdio_mcp_forbidden", str(name)))
        elif config.get("type") != "http":
            violations.append(PolicyViolation(agent_id, path, "unsupported_mcp_transport", str(name)))
        elif not _valid_http_url(config.get("url")):
            violations.append(PolicyViolation(agent_id, path, "invalid_mcp_url", str(name)))
    if agent_id == SECURITY_OPERATIONS_EXPERT_AGENT_ID and "sec-ops" not in servers:
        violations.append(PolicyViolation(agent_id, path, "security_mcp_missing", "sec-ops"))
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
    """Validate an API/candidate MCP document against transport and seed-owned servers."""

    violations = list(validate_mcp_content(content, agent_id=agent_id))
    templates = template_dir or default_runtime_template_dir()
    template_workspace = templates / "data" / "business-agents" / agent_id / "workspace"
    template_path = template_workspace / _MANAGED_MCP_PATH
    template_mcp, template_violation = _read_regular_text(
        template_path,
        anchor=template_workspace,
        agent_id=agent_id,
        required=False,
    )
    if template_violation:
        violations.append(PolicyViolation(agent_id, _MANAGED_MCP_PATH, "managed_mcp_template_invalid", template_violation.rule_id))
        return tuple(violations)
    if template_mcp is None:
        return tuple(violations)
    try:
        context = build_render_context(mode=runtime_mode, env=env, runtime_root=runtime_root)
        rendered = render_template_file(
            template_mcp,
            rel_path=Path(_MANAGED_MCP_PATH),
            context=context,
        )
        current_data = json.loads(content)
        desired_data = json.loads(rendered)
        current_servers = current_data.get("mcpServers", {})
        desired_servers = desired_data.get("mcpServers", {})
        for name, desired in desired_servers.items():
            current = current_servers.get(name) if isinstance(current_servers, dict) else None
            if not isinstance(current, dict) or current.get("type") != desired.get("type") or current.get("url") != desired.get("url"):
                violations.append(PolicyViolation(agent_id, ".mcp.json", "managed_mcp_server_drift", str(name)))
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        violations.append(PolicyViolation(agent_id, ".mcp.json", "managed_mcp_template_invalid", exc.__class__.__name__))
    return tuple(violations)


def validate_security_agent_yaml(text: str, *, path: str) -> tuple[PolicyViolation, ...]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return (PolicyViolation(SECURITY_OPERATIONS_EXPERT_AGENT_ID, path, "invalid_agent_yaml", exc.__class__.__name__),)
    agent = data.get("agent") if isinstance(data, dict) else None
    approval = data.get("approval_policy") if isinstance(data, dict) else None
    violations: list[PolicyViolation] = []
    if not isinstance(agent, dict) or agent.get("profile") != SECURITY_OPERATIONS_EXPERT_AGENT_ID:
        violations.append(PolicyViolation(SECURITY_OPERATIONS_EXPERT_AGENT_ID, path, "security_agent_identity", "profile"))
    if isinstance(agent, dict) and "requires_web_hitl" in agent:
        violations.append(PolicyViolation(SECURITY_OPERATIONS_EXPERT_AGENT_ID, path, "duplicate_hitl_policy", "requires_web_hitl"))
    if not isinstance(approval, dict) or approval.get("phase_field") != "phase" or approval.get("proposal_side_effects") != "deny":
        violations.append(PolicyViolation(SECURITY_OPERATIONS_EXPERT_AGENT_ID, path, "security_approval_contract", "phase/proposal"))
    return tuple(violations)


def policy_projection(plans: Iterable[WorkspacePolicyPlan]) -> str:
    projection: list[_PolicyProjectionEntry] = []
    for plan in sorted(plans, key=lambda item: item.agent_id):
        projection.append(
            {
                "agent_id": plan.agent_id,
                "compliant": plan.is_compliant,
                "changes": [(change.path.name, change.rule_id, change.after_sha256) for change in plan.changes],
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
    """Validate one live/candidate workspace without mutating it.

    A planned reconciliation is drift at an execution boundary: startup may migrate a
    clean repository, while SDK execution, publication, and rollback must fail closed.
    """

    templates = template_dir or default_runtime_template_dir()
    seed_workspace = templates / "data" / "business-agents" / agent_id / "workspace"
    context = build_render_context(mode=runtime_mode, env=env, runtime_root=runtime_root)
    plan = plan_workspace_policy(
        workspace=workspace,
        agent_id=agent_id,
        template_workspace=seed_workspace if seed_workspace.is_dir() else None,
        render_context=context,
    )
    drift = tuple(
        PolicyViolation(
            agent_id,
            change.path.as_posix(),
            "managed_policy_drift",
            change.rule_id,
        )
        for change in plan.changes
    )
    return (*plan.violations, *drift)


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
    explicit = os.environ.get("RUNTIME_VOLUME_SEEDS_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "docker" / "runtime-volume-seeds"
