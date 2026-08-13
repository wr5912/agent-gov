#!/usr/bin/env python3
"""Validate the authorized defensive boundary of built-in business Agents."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

DISALLOWED_RUNTIME_TOOLS = frozenset({"Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"})
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
SAFE_ASSET_TOOLS = frozenset({"Skill", "Task"})
SECURITY_OPERATIONS_EXPERT_ID = "security-operations-expert"
SECURITY_OPERATIONS_EXPERT_REVIEWED_TASKS = frozenset(
    {
        "response-playbook-builder",
        "response-playbook-planning",
    }
)
SECURITY_OPERATIONS_EXPERT_ALLOWED_RULES = frozenset(
    {
        "Skill",
        "Task(response-playbook-builder)",
        "Task(response-playbook-planning)",
        "Edit(/data/outputs/security-operations-expert/**)",
        "Write(/data/outputs/security-operations-expert/**)",
    }
)
TASK_PERMISSION = re.compile(r"^Task\(([a-z0-9]+(?:-[a-z0-9]+)*)\)$")
SHELL_FENCE = re.compile(r"```(?:bash|sh|shell|zsh|powershell|cmd)\b", re.IGNORECASE)
SENSITIVE_AGENT_DENIES = frozenset(
    {
        "Read(./.env)",
        "Read(./.env.*)",
        "Read(./secrets/**)",
        "Read(./.mcp.json)",
        "Read(./.mcp.*.json)",
        "Read(./.claude/settings.json)",
        "Read(./.claude/settings.*.json)",
    }
)
GOVERNOR_ALLOWED_RULES = frozenset(
    {
        "Skill",
        "Read(/**/business-agents/*/workspace/CLAUDE.md)",
        "Read(/**/business-agents/*/workspace/.claude/skills/*/SKILL.md)",
        "Read(/**/business-agents/*/workspace/.claude/agents/*.md)",
        "Read(/**/business-agents/*/workspace/.claude/rules/*.md)",
        "Read(/**/business-agents/*/workspace/.claude/commands/*.md)",
    }
)
GOVERNOR_REQUIRED_DENIES = frozenset(
    {
        "Write",
        "Edit",
        "NotebookEdit",
        "Bash",
        "WebFetch",
        "WebSearch",
        "mcp__*",
        "Read(/**/.env)",
        "Read(/**/.env.*)",
        "Read(/**/secrets/**)",
        "Read(/**/.mcp.json)",
        "Read(/**/.mcp.*.json)",
        "Read(/**/.claude/settings.json)",
        "Read(/**/.claude/settings.*.json)",
        "Read(/**/claude-root/**)",
        "Read(/**/claude-roots/**)",
        "Read(/**/.git/**)",
    }
)
APPROVED_AGENT_HOOKS = {
    "PreToolUse": (
        "Edit|Write|NotebookEdit",
        'python "$CLAUDE_PROJECT_DIR/hooks/pre_tool_guard.py"',
    ),
    "SessionStart": (
        "startup|resume",
        'python "$CLAUDE_PROJECT_DIR/hooks/session_start.py"',
    ),
}
DISALLOWED_HOOK_IMPORTS = frozenset({"httpx", "requests", "socket", "subprocess", "urllib"})
DISALLOWED_HOOK_CALLS = frozenset({"eval", "exec", "__import__"})
APPROVED_AGENT_HOOK_DIGESTS = {
    "hooks/pre_tool_guard.py": "8869c67d30d6f1269665c2ce0a6217a386a708c4b16e5c68e2216f55029ed959",
    "hooks/session_start.py": "38f7cab950b539311dbdaf16382ad7da81f492314dd3a5f8c019a9ad6d795c7a",
}


@dataclass(frozen=True)
class BoundaryIssue:
    path: str
    message: str

    def format(self) -> str:
        return f"FAIL: {self.path}: {self.message}"


class _JsonObject(dict[str, object]):
    """Owned JSON object boundary for this standalone checker."""


def _read_json(path: Path, *, root: Path, issues: list[BoundaryIssue]) -> _JsonObject | None:
    rel_path = path.relative_to(root).as_posix()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(BoundaryIssue(rel_path, f"JSON unreadable: {exc.__class__.__name__}"))
        return None
    if not isinstance(value, dict):
        issues.append(BoundaryIssue(rel_path, "top-level JSON must be an object"))
        return None
    return _JsonObject(value)


def _read_yaml(path: Path, *, root: Path, issues: list[BoundaryIssue]) -> _JsonObject | None:
    rel_path = path.relative_to(root).as_posix()
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        issues.append(BoundaryIssue(rel_path, f"YAML unreadable: {exc.__class__.__name__}"))
        return None
    if not isinstance(value, dict):
        issues.append(BoundaryIssue(rel_path, "top-level YAML must be an object"))
        return None
    return _JsonObject(value)


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _hook_matchers(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    matchers: list[str] = []
    for entries in value.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("matcher"), str):
                matchers.append(entry["matcher"])
    return tuple(matchers)


def _tool_name(rule: str) -> str:
    return rule.partition("(")[0]


def _reviewed_task_asset_issues(
    root: Path,
    workspace: Path,
    *,
    task_name: str,
) -> list[BoundaryIssue]:
    path = workspace / ".claude/agents" / f"{task_name}.md"
    rel_path = path.relative_to(root).as_posix()
    if path.is_symlink() or not path.is_file():
        return [BoundaryIssue(rel_path, "reviewed Task agent must be a real tracked asset")]
    issues: list[BoundaryIssue] = []
    metadata = _frontmatter(path, root=root, issues=issues)
    if metadata is None:
        return issues
    if metadata.get("name") != task_name:
        issues.append(BoundaryIssue(rel_path, "reviewed Task agent name must match its permission target"))
    if metadata.get("tools") != []:
        issues.append(BoundaryIssue(rel_path, "reviewed Task agent must declare tools: []"))
    return issues


def _security_operations_task_issues(
    root: Path,
    workspace: Path,
    *,
    allow: list[str],
    rel_path: str,
) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    referenced_tasks: set[str] = set()
    for rule in allow:
        if _tool_name(rule) != "Task":
            continue
        match = TASK_PERMISSION.fullmatch(rule)
        if match is None:
            issues.append(BoundaryIssue(rel_path, "Task permission must name one reviewed subagent exactly"))
            continue
        task_name = match.group(1)
        referenced_tasks.add(task_name)
        if task_name not in SECURITY_OPERATIONS_EXPERT_REVIEWED_TASKS:
            issues.append(BoundaryIssue(rel_path, "Task permission references an unapproved subagent"))
    if SECURITY_OPERATIONS_EXPERT_REVIEWED_TASKS - referenced_tasks:
        issues.append(BoundaryIssue(rel_path, "reviewed Task permission is missing"))
    for task_name in sorted(SECURITY_OPERATIONS_EXPERT_REVIEWED_TASKS & referenced_tasks):
        issues.extend(_reviewed_task_asset_issues(root, workspace, task_name=task_name))
    return issues


def _security_operations_allowlist_issues(
    root: Path,
    workspace: Path,
    *,
    allow: list[str],
    rel_path: str,
) -> list[BoundaryIssue]:
    issues = _security_operations_task_issues(root, workspace, allow=allow, rel_path=rel_path)
    if len(allow) != len(SECURITY_OPERATIONS_EXPERT_ALLOWED_RULES) or set(allow) != SECURITY_OPERATIONS_EXPERT_ALLOWED_RULES:
        issues.append(BoundaryIssue(rel_path, "security-operations-expert permissions.allow must match the reviewed exact set"))
    return issues


def _permission_mode_issues(permissions: Mapping[str, object], *, rel_path: str) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    if permissions.get("defaultMode") != "dontAsk":
        issues.append(BoundaryIssue(rel_path, "defaultMode must be dontAsk"))
    if permissions.get("disableBypassPermissionsMode") != "disable":
        issues.append(BoundaryIssue(rel_path, "bypass permissions mode must be disabled"))
    if _string_list(permissions.get("ask")):
        issues.append(BoundaryIssue(rel_path, "built-in business Agent must not keep interactive permission rules"))
    return issues


def _runtime_tool_permission_issues(
    *,
    allow: list[str],
    deny: list[str],
    rel_path: str,
) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    for tool in DISALLOWED_RUNTIME_TOOLS:
        if any(_tool_name(rule) == tool for rule in allow):
            issues.append(BoundaryIssue(rel_path, f"built-in business Agent must not allow {tool}"))
        if tool not in deny:
            issues.append(BoundaryIssue(rel_path, f"built-in business Agent must deny {tool}"))
    return issues


def _allow_rule_issues(
    *,
    allow: list[str],
    agent_id: str,
    rel_path: str,
) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    output_glob = f"/data/outputs/{agent_id}/**"
    approved_writes = {f"Edit({output_glob})", f"Write({output_glob})"}
    for rule in allow:
        if _tool_name(rule) in WRITE_TOOLS and rule not in approved_writes:
            issues.append(BoundaryIssue(rel_path, "write permission must stay inside the Agent output directory"))
        if rule.startswith("mcp__"):
            issues.append(BoundaryIssue(rel_path, "MCP permission must remain disabled until an exact capability contract is active"))
    return issues


def _permission_issues(
    root: Path,
    workspace: Path,
    data: _JsonObject,
    *,
    rel_path: str,
    agent_id: str,
) -> list[BoundaryIssue]:
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return [BoundaryIssue(rel_path, "permissions must be an object")]
    issues: list[BoundaryIssue] = []
    allow = _string_list(permissions.get("allow"))
    deny = _string_list(permissions.get("deny"))
    issues.extend(_permission_mode_issues(permissions, rel_path=rel_path))
    issues.extend(_runtime_tool_permission_issues(allow=allow, deny=deny, rel_path=rel_path))
    if not SENSITIVE_AGENT_DENIES.issubset(deny):
        issues.append(BoundaryIssue(rel_path, "built-in business Agent must deny raw credentials and configuration files"))
    if "mcp__*" not in deny:
        issues.append(BoundaryIssue(rel_path, "built-in business Agent must deny MCP until an exact capability contract is active"))
    issues.extend(_allow_rule_issues(allow=allow, agent_id=agent_id, rel_path=rel_path))
    if agent_id == SECURITY_OPERATIONS_EXPERT_ID:
        issues.extend(_security_operations_allowlist_issues(root, workspace, allow=allow, rel_path=rel_path))
    routed_tools = {tool for matcher in _hook_matchers(data.get("hooks")) for tool in matcher.split("|")}
    if routed_tools & DISALLOWED_RUNTIME_TOOLS:
        issues.append(BoundaryIssue(rel_path, "hook matcher must not route shell or Web tools"))
    issues.extend(_hook_configuration_issues(data.get("hooks"), rel_path=rel_path))
    return issues


def _hook_configuration_issues(value: object, *, rel_path: str) -> list[BoundaryIssue]:
    if not isinstance(value, dict) or set(value) != set(APPROVED_AGENT_HOOKS):
        return [BoundaryIssue(rel_path, "hooks must contain only approved local governance entrypoints")]
    issues: list[BoundaryIssue] = []
    for event, (matcher, command) in APPROVED_AGENT_HOOKS.items():
        entries = value.get(event)
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            issues.append(BoundaryIssue(rel_path, f"{event} must contain one approved hook entry"))
            continue
        entry = entries[0]
        hooks = entry.get("hooks")
        valid_hook = isinstance(hooks, list) and len(hooks) == 1 and isinstance(hooks[0], dict)
        if entry.get("matcher") != matcher or not valid_hook:
            issues.append(BoundaryIssue(rel_path, f"{event} hook shape must match the approved entrypoint"))
            continue
        hook = hooks[0]
        timeout = hook.get("timeout")
        if hook.get("type") != "command" or hook.get("command") != command or not isinstance(timeout, int) or not 0 < timeout <= 10:
            issues.append(BoundaryIssue(rel_path, f"{event} hook command must match the approved bounded entrypoint"))
    return issues


def _approved_hook_digest_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    for relative_path, expected_digest in APPROVED_AGENT_HOOK_DIGESTS.items():
        path = workspace / relative_path
        rel_path = path.relative_to(root).as_posix()
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            issues.append(BoundaryIssue(rel_path, f"approved hook source unreadable: {exc.__class__.__name__}"))
            continue
        if digest != expected_digest:
            issues.append(BoundaryIssue(rel_path, "approved hook source digest changed and requires boundary review"))
    return issues


def _hook_ast_node_issue(node: ast.AST, *, rel_path: str) -> BoundaryIssue | None:
    if isinstance(node, ast.Import):
        names = {alias.name.split(".", 1)[0] for alias in node.names}
        if names & DISALLOWED_HOOK_IMPORTS:
            return BoundaryIssue(rel_path, "hook source must not import process or network clients")
    if isinstance(node, ast.ImportFrom) and (node.module or "").split(".", 1)[0] in DISALLOWED_HOOK_IMPORTS:
        return BoundaryIssue(rel_path, "hook source must not import process or network clients")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in DISALLOWED_HOOK_CALLS:
        return BoundaryIssue(rel_path, "hook source must not use dynamic execution")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "os" and node.func.attr in {"system", "popen"}:
            return BoundaryIssue(rel_path, "hook source must not launch shell commands")
    return None


def _hook_source_content_issue(root: Path, path: Path) -> BoundaryIssue | None:
    rel_path = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return BoundaryIssue(rel_path, f"hook source unreadable: {exc.__class__.__name__}")
    for node in ast.walk(tree):
        issue = _hook_ast_node_issue(node, rel_path=rel_path)
        if issue is not None:
            return issue
    return None


def _hook_source_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues = _approved_hook_digest_issues(root, workspace)
    for path in sorted((workspace / "hooks").glob("*.py")):
        issue = _hook_source_content_issue(root, path)
        if issue is not None:
            issues.append(issue)
    return issues


def _sandbox_issues(data: _JsonObject, *, rel_path: str, agent_id: str) -> list[BoundaryIssue]:
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, dict):
        return [BoundaryIssue(rel_path, "sandbox must be an object")]
    issues: list[BoundaryIssue] = []
    if not (
        sandbox.get("enabled") is True
        and sandbox.get("failIfUnavailable") is True
        and sandbox.get("autoAllowBashIfSandboxed") is False
        and sandbox.get("allowUnsandboxedCommands") is False
    ):
        issues.append(BoundaryIssue(rel_path, "sandbox must be enabled, fail-closed, and never grant shell access"))
    filesystem = sandbox.get("filesystem")
    expected_output = f"/data/outputs/{agent_id}"
    if not isinstance(filesystem, dict) or _string_list(filesystem.get("allowWrite")) != [expected_output]:
        issues.append(BoundaryIssue(rel_path, "sandbox write scope must be the exact Agent output directory"))
    network = sandbox.get("network")
    if not isinstance(network, dict) or _string_list(network.get("allowedDomains")) or _string_list(network.get("deniedDomains")) != ["*"]:
        issues.append(BoundaryIssue(rel_path, "sandbox network must deny every domain"))
    deny_read = _string_list(filesystem.get("denyRead")) if isinstance(filesystem, dict) else []
    expected_denies = {"./**", "/data/uploads/**", f"/data/business-agents/{agent_id}/claude-root/**"}
    if not expected_denies.issubset(deny_read):
        issues.append(BoundaryIssue(rel_path, "sandbox must deny Workspace, uploads, and Agent runtime metadata reads"))
    return issues


def _settings_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    path = workspace / ".claude/settings.json"
    data = _read_json(path, root=root, issues=issues)
    if data is None:
        return issues
    rel_path = path.relative_to(root).as_posix()
    agent_id = workspace.parent.name
    issues.extend(_permission_issues(root, workspace, data, rel_path=rel_path, agent_id=agent_id))
    issues.extend(_sandbox_issues(data, rel_path=rel_path, agent_id=agent_id))
    return issues


def _mcp_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    path = workspace / ".mcp.json"
    data = _read_json(path, root=root, issues=issues)
    if data is None:
        return issues
    rel_path = path.relative_to(root).as_posix()
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return [*issues, BoundaryIssue(rel_path, "mcpServers must be an object")]
    if servers:
        issues.append(BoundaryIssue(rel_path, "built-in business Agent MCP discovery must remain empty until the P0-MCP gate is active"))
    for name, config in servers.items():
        if not isinstance(config, dict):
            issues.append(BoundaryIssue(rel_path, f"MCP server {name!s} must be an object"))
            continue
        if config.get("type") != "http" or not isinstance(config.get("url"), str):
            issues.append(BoundaryIssue(rel_path, f"MCP server {name!s} must use an HTTP URL"))
        if "command" in config or "args" in config:
            issues.append(BoundaryIssue(rel_path, f"MCP server {name!s} must not execute a local command"))
    return issues


def _frontmatter(path: Path, *, root: Path, issues: list[BoundaryIssue]) -> _JsonObject | None:
    rel_path = path.relative_to(root).as_posix()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        issues.append(BoundaryIssue(rel_path, f"asset unreadable: {exc.__class__.__name__}"))
        return None
    if not lines or lines[0] != "---":
        issues.append(BoundaryIssue(rel_path, "Agent asset must declare YAML frontmatter"))
        return None
    try:
        end = lines.index("---", 1)
        value = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError) as exc:
        issues.append(BoundaryIssue(rel_path, f"frontmatter unreadable: {exc.__class__.__name__}"))
        return None
    if not isinstance(value, dict):
        issues.append(BoundaryIssue(rel_path, "frontmatter must be an object"))
        return None
    return _JsonObject(value)


def _asset_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    asset_root = workspace / ".claude"
    for path in sorted(asset_root.rglob("*.md")):
        rel_path = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(BoundaryIssue(rel_path, f"asset unreadable: {exc.__class__.__name__}"))
            continue
        if SHELL_FENCE.search(text):
            issues.append(BoundaryIssue(rel_path, "Agent asset must not embed executable shell blocks"))
    governed_assets = [
        *sorted((asset_root / "agents").glob("*.md")),
        *sorted((asset_root / "commands").glob("*.md")),
        *sorted((asset_root / "skills").glob("*/SKILL.md")),
    ]
    for path in governed_assets:
        metadata = _frontmatter(path, root=root, issues=issues)
        if metadata is None:
            continue
        raw_tools = metadata.get("allowed-tools", metadata.get("tools"))
        tools = _string_list(raw_tools)
        if not isinstance(raw_tools, list) or len(tools) != len(raw_tools):
            issues.append(BoundaryIssue(path.relative_to(root).as_posix(), "asset tools must be a string list"))
            continue
        for tool in tools:
            if tool not in SAFE_ASSET_TOOLS:
                issues.append(
                    BoundaryIssue(
                        path.relative_to(root).as_posix(),
                        "asset tool must be read-only or a platform planning primitive",
                    )
                )
    return issues


def _agent_manifest_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    path = workspace / "agent.yaml"
    data = _read_yaml(path, root=root, issues=issues)
    if data is None:
        return issues
    rel_path = path.relative_to(root).as_posix()
    agent = data.get("agent")
    if not isinstance(agent, dict) or agent.get("id") != workspace.parent.name:
        issues.append(BoundaryIssue(rel_path, "agent.id must exactly match the built-in Agent directory"))
    approval = data.get("approval_policy")
    if not isinstance(approval, dict):
        return [*issues, BoundaryIssue(rel_path, "approval_policy must be an object")]
    required_values = {
        "agent_soc_side_effects": "deny",
        "proposal_side_effects": "deny",
        "lifecycle_worker_owns_system_changes": True,
        "agent_monitors_execution": False,
        "agent_closes_response_case": False,
    }
    for key, expected in required_values.items():
        if approval.get(key) != expected:
            issues.append(BoundaryIssue(rel_path, f"approval_policy.{key} must be {expected!r}"))
    mcp = data.get("mcp")
    if not isinstance(mcp, dict) or mcp.get("status") != "disabled_pending_exact_capability_contract" or mcp.get("servers") != {}:
        issues.append(BoundaryIssue(rel_path, "mcp must remain disabled until the exact capability contract is active"))
    return issues


def _governor_permission_issues(permissions: Mapping[str, object], *, rel_path: str) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    if permissions.get("defaultMode") != "dontAsk" or permissions.get("disableBypassPermissionsMode") != "disable":
        issues.append(BoundaryIssue(rel_path, "Governor permissions must stay non-interactive and disable bypass"))
    if _string_list(permissions.get("ask")):
        issues.append(BoundaryIssue(rel_path, "Governor ask rules must remain empty"))
    if set(_string_list(permissions.get("allow"))) != GOVERNOR_ALLOWED_RULES:
        issues.append(BoundaryIssue(rel_path, "Governor allowlist must contain only non-sensitive instruction assets"))
    deny = set(_string_list(permissions.get("deny")))
    if not GOVERNOR_REQUIRED_DENIES.issubset(deny):
        issues.append(BoundaryIssue(rel_path, "Governor must deny writes, network, credentials, and runtime metadata"))
    return issues


def _governor_mcp_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    mcp_path = workspace / ".mcp.json"
    mcp_data = _read_json(mcp_path, root=root, issues=issues)
    if mcp_data != {"mcpServers": {}}:
        issues.append(BoundaryIssue(mcp_path.relative_to(root).as_posix(), "Governor MCP discovery must remain empty"))
    return issues


def _governor_sandbox_issues(data: _JsonObject, *, rel_path: str) -> list[BoundaryIssue]:
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, dict):
        return [BoundaryIssue(rel_path, "Governor sandbox must be an object")]
    filesystem = sandbox.get("filesystem")
    network = sandbox.get("network")
    valid = (
        sandbox.get("enabled") is True
        and sandbox.get("failIfUnavailable") is True
        and sandbox.get("autoAllowBashIfSandboxed") is False
        and sandbox.get("allowUnsandboxedCommands") is False
        and isinstance(filesystem, dict)
        and _string_list(filesystem.get("allowWrite")) == []
        and isinstance(network, dict)
        and _string_list(network.get("allowedDomains")) == []
        and _string_list(network.get("deniedDomains")) == ["*"]
    )
    return [] if valid else [BoundaryIssue(rel_path, "Governor sandbox must deny writes, shell access, and all network")]


def _governor_contract_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    contracts = {
        workspace / "CLAUDE.md": ("只消费后端提供的脱敏 typed context", "不使用 WebFetch、WebSearch、Bash、MCP"),
        workspace / ".claude/skills/read-business-agent-config/SKILL.md": (
            "allowed-tools:\n  - Read",
            "不得用 Read、Glob 或 Grep 在文件系统中寻找同名文件",
        ),
    }
    for contract, markers in contracts.items():
        contract_rel = contract.relative_to(root).as_posix()
        try:
            text = contract.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(BoundaryIssue(contract_rel, f"Governor contract unreadable: {exc.__class__.__name__}"))
            continue
        for marker in markers:
            if marker not in text:
                issues.append(BoundaryIssue(contract_rel, f"missing Governor boundary marker: {marker}"))
    return issues


def _governor_issues(root: Path) -> list[BoundaryIssue]:
    workspace = root / "docker/runtime-bootstrap/governor-workspace"
    issues: list[BoundaryIssue] = []
    path = workspace / ".claude/settings.json"
    data = _read_json(path, root=root, issues=issues)
    if data is None:
        return issues
    rel_path = path.relative_to(root).as_posix()
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return [*issues, BoundaryIssue(rel_path, "Governor permissions must be an object")]
    issues.extend(_governor_permission_issues(permissions, rel_path=rel_path))
    issues.extend(_governor_mcp_issues(root, workspace))
    issues.extend(_governor_sandbox_issues(data, rel_path=rel_path))
    issues.extend(_governor_contract_issues(root, workspace))
    return issues


def _contract_issues(root: Path, workspace: Path) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    contracts = {workspace / "CLAUDE.md": ("只读候选提供者", "仅限已授权环境的防御性安全运营")}
    for path, markers in contracts.items():
        rel_path = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(BoundaryIssue(rel_path, f"contract unreadable: {exc.__class__.__name__}"))
            continue
        for marker in markers:
            if marker not in text:
                issues.append(BoundaryIssue(rel_path, f"missing defensive contract marker: {marker}"))
    return issues


def collect_issues(root: Path) -> tuple[list[BoundaryIssue], int]:
    agents_root = root / "docker/runtime-bootstrap/business-agents"
    if not agents_root.is_dir():
        return [BoundaryIssue(agents_root.relative_to(root).as_posix(), "no built-in business Agent workspace found")], 0
    agent_dirs = sorted(path for path in agents_root.iterdir() if path.is_dir())
    if not agent_dirs:
        return [BoundaryIssue(agents_root.relative_to(root).as_posix(), "no built-in business Agent workspace found")], 0
    issues: list[BoundaryIssue] = []
    issues.extend(_governor_issues(root))
    for agent_dir in agent_dirs:
        rel_path = agent_dir.relative_to(root).as_posix()
        if agent_dir.is_symlink():
            issues.append(BoundaryIssue(rel_path, "built-in Agent directory must not be a symlink"))
            continue
        workspace = agent_dir / "workspace"
        if not workspace.is_dir() or workspace.is_symlink():
            issues.append(BoundaryIssue(rel_path, "built-in Agent must contain a real workspace directory"))
            continue
        issues.extend(_settings_issues(root, workspace))
        issues.extend(_mcp_issues(root, workspace))
        issues.extend(_hook_source_issues(root, workspace))
        issues.extend(_asset_issues(root, workspace))
        issues.extend(_agent_manifest_issues(root, workspace))
        issues.extend(_contract_issues(root, workspace))
    return issues, len(agent_dirs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check built-in business Agent defensive boundaries.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    issues, agent_count = collect_issues(root)
    if issues:
        for issue in issues:
            print(issue.format())
        return 1
    print(f"DEFENSIVE_SECURITY_BOUNDARY_OK: agents={agent_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
