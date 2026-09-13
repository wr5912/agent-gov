"""把版本化 Harness subagents 转为 AgentScope 公共模板。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, TypeAlias, cast

import yaml
from agentgov_subagent_manifest_policy import validate_subagent_manifest
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app import SubAgentTemplate
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionMode, PermissionRule

from .types import JsonObject

_DIGEST = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,126}")
_SNAPSHOT_SOURCE = re.compile(r"(?:candidate|published)-[0-9a-f]{48}")
_VERSION = re.compile(r"[0-9a-f]+")
_SNAPSHOT_MARKER = "snapshot.json"
SubagentTemplateRegistry: TypeAlias = dict[str, SubAgentTemplate]
PermissionRuleRegistry: TypeAlias = dict[str, list[PermissionRule]]
_LOGGER = logging.getLogger(__name__)


class InvalidSubagentManifestError(ValueError):
    """单个 Harness 的 subagent 契约无效；该 Harness 仍不可绑定运行。"""


def load_subagent_templates(
    workspace: Path,
    content_digest: str,
) -> SubagentTemplateRegistry:
    """加载一个物化 Workspace 的显式 subagent 模板。"""

    if _DIGEST.fullmatch(content_digest) is None:
        raise ValueError("Subagent template namespace requires a full Harness digest")
    root = workspace / "subagents"
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("subagents must be a non-symlink directory")
    templates: dict[str, SubAgentTemplate] = {}
    for subagent in sorted(root.iterdir()):
        if subagent.is_symlink() or not subagent.is_dir():
            raise ValueError("Every subagent entry must be a non-symlink directory")
        template = _load_template(subagent, content_digest)
        if template.type in templates:
            raise ValueError(f"Duplicate subagent template type: {template.type}")
        templates[template.type] = template
    return templates


def discover_subagent_templates(
    snapshots_root: Path,
) -> SubagentTemplateRegistry:
    """仅扫描 API 原子物化且在 Runtime 启动前存在的不可变快照。"""

    templates: dict[str, SubAgentTemplate] = {}
    if not snapshots_root.exists():
        return templates
    if snapshots_root.is_symlink() or not snapshots_root.is_dir():
        raise ValueError("Harness snapshot root must be a non-symlink directory")
    for snapshot in sorted(snapshots_root.iterdir()):
        if _SNAPSHOT_SOURCE.fullmatch(snapshot.name) is None:
            continue
        workspace, digest = _validated_snapshot(snapshot)
        try:
            incoming = load_subagent_templates(workspace, digest)
        except InvalidSubagentManifestError:
            # 仅隔离经过 marker 与完整 digest 校验的旧版本契约错误。
            # Workspace 绑定时仍会重新校验并拒绝该 Harness；不得注册降级模板。
            _LOGGER.warning(
                "Immutable Harness snapshot %s has invalid subagent manifest; its workspace bindings remain unavailable",
                snapshot.name,
            )
            continue
        _merge_templates(
            templates,
            incoming,
        )
    return templates


def _validated_snapshot(snapshot: Path) -> tuple[Path, str]:
    marker = snapshot / _SNAPSHOT_MARKER
    workspace = snapshot / "workspace"
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("Harness snapshot must be a non-symlink directory")
    if marker.is_symlink() or not marker.is_file() or workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("Harness snapshot is incomplete or unsafe")
    if {entry.name for entry in snapshot.iterdir()} != {_SNAPSHOT_MARKER, "workspace"}:
        raise ValueError("Harness snapshot contains unexpected entries")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Harness snapshot marker is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Harness snapshot marker must be an object")
    digest = payload.get("harness_digest")
    agent_id = payload.get("agent_id")
    version_id = payload.get("agent_version_id")
    if (
        set(payload)
        != {
            "schema_version",
            "source_id",
            "agent_id",
            "agent_version_id",
            "harness_digest",
        }
        or payload.get("schema_version") != 1
        or payload.get("source_id") != snapshot.name
        or not isinstance(agent_id, str)
        or _NAME.fullmatch(agent_id) is None
        or not isinstance(version_id, str)
        or _VERSION.fullmatch(version_id) is None
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
    ):
        raise ValueError("Harness snapshot marker does not match its immutable tuple")
    from .workspace_manager import harness_digest

    if harness_digest(workspace) != digest:
        raise ValueError("Harness snapshot digest changed")
    return workspace, digest


def _load_template(subagent: Path, digest: str) -> SubAgentTemplate:
    manifest = _load_yaml_object(subagent / "agent.yaml")
    issues = validate_subagent_manifest(manifest, directory_name=subagent.name)
    if issues:
        details = "; ".join(f"{issue.code}: {issue.detail}" for issue in issues)
        raise InvalidSubagentManifestError(f"Invalid subagent manifest: {details}")
    agent = cast(JsonObject, manifest["agent"])
    policy = cast(JsonObject, manifest["workspace_policy"])
    session = cast(JsonObject, manifest["session"])
    name = cast(str, agent["id"])
    description = cast(str, agent["description"])
    prompt_path = subagent / "AGENT.md"
    if prompt_path.is_symlink() or not prompt_path.is_file():
        raise ValueError("Subagent AGENT.md is missing or unsafe")
    mode = _permission_mode(session["permission_mode"])
    permission = PermissionContext(
        mode=mode,
        allow_rules=_permission_rules(
            policy.get("allowed_tools"),
            PermissionBehavior.ALLOW,
            source=f"agentgov-subagent:{digest}:{name}",
        ),
        deny_rules=_permission_rules(
            policy.get("denied_tools"),
            PermissionBehavior.DENY,
            source=f"agentgov-subagent:{digest}:{name}",
        ),
    )
    return SubAgentTemplate(
        type=f"agentgov-{digest}-{name}",
        description=description.strip(),
        system_prompt_template=_escape_prompt(prompt_path.read_text(encoding="utf-8")),
        context_config=ContextConfig.model_validate(manifest.get("context_config", {})),
        react_config=ReActConfig.model_validate(manifest.get("react_config", {})),
        permission_context=permission,
        override_leader_mode=True,
        extend_leader_permission_rules=False,
        extend_leader_working_directories=True,
    )


def _load_yaml_object(path: Path) -> JsonObject:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Subagent agent.yaml is missing or unsafe")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("Subagent agent.yaml is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("Subagent agent.yaml must be an object")
    return value


def _permission_mode(value: Any) -> PermissionMode:
    try:
        mode = PermissionMode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Subagent permission_mode is invalid") from exc
    if mode is PermissionMode.BYPASS:
        raise ValueError("AgentGov Runtime forbids bypass permission mode")
    return mode


def _permission_rules(
    value: Any,
    behavior: PermissionBehavior,
    *,
    source: str,
) -> PermissionRuleRegistry:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Subagent tool policy must be a string list")
    grouped: dict[str, list[PermissionRule]] = {}
    for item in value:
        name, separator, content = item.partition("(")
        if behavior is PermissionBehavior.ALLOW and name.startswith("mcp__") and any(character in name for character in "*?["):
            raise ValueError("Subagent allow policy cannot wildcard MCP tools")
        if separator:
            if not item.endswith(")") or not content[:-1]:
                raise ValueError("Subagent tool policy contains an invalid rule")
        if not name:
            raise ValueError("Subagent tool policy contains an empty tool name")
        grouped.setdefault(name, []).append(
            PermissionRule(
                tool_name=name,
                rule_content=content[:-1] if separator else None,
                behavior=behavior,
                source=source,
            ),
        )
    return grouped


def _escape_prompt(value: str) -> str:
    return value.replace("{", "{{").replace("}", "}}")


def _merge_templates(
    target: dict[str, SubAgentTemplate],
    incoming: dict[str, SubAgentTemplate],
) -> None:
    for template_type, template in incoming.items():
        existing = target.get(template_type)
        if existing is not None and existing != template:
            raise ValueError(f"Conflicting subagent template: {template_type}")
        target[template_type] = template
