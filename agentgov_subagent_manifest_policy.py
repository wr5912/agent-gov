"""AgentGov API 与 AgentScope Runtime 共用的 Subagent manifest 安全契约。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT
from pydantic import BaseModel, Field, ValidationError

WORKER_PERMISSION_MODE = "dont_ask"
_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,126}")
_TOOL_POLICY_FIELDS = ("allowed_tools", "ask_tools", "denied_tools")


class _ContextConfigContract(BaseModel):
    """AgentScope 2.0.8 ``ContextConfig`` 的无 Runtime 依赖校验镜像。"""

    trigger_ratio: float = Field(default=0.8, gt=0, le=0.9)
    reserve_ratio: float = Field(default=0.1, gt=0, lt=0.9)
    context_buffer_ratio: float = Field(default=0.2, ge=0, le=1)
    compression_prompt: str = ""
    summary_template: str = ""
    summary_schema: dict[Any, Any] = Field(default_factory=dict)
    tool_result_limit: int = 50000
    compression_fallback_to_truncation: bool = True
    compression_tool_enabled: bool = False
    max_image_num: int = Field(default=5, ge=0)


class _ReActConfigContract(BaseModel):
    """AgentScope 2.0.8 ``ReActConfig`` 的无 Runtime 依赖校验镜像。"""

    max_iters: int = 50
    structured_output_grace_iters: int = Field(default=5, gt=0)
    stop_on_reject: bool = False
    interruption_message: str = "I notice the interruption. How can I help you?"
    interruption_raise_cancelled_error: bool = False


@dataclass(frozen=True)
class SubagentManifestIssue:
    """与具体调用方错误类型解耦的 manifest 违规。"""

    code: str
    detail: str


def validate_subagent_manifest(
    value: object,
    *,
    directory_name: str,
) -> tuple[SubagentManifestIssue, ...]:
    """校验无人值守 Subagent 的静态身份、Runtime 与权限边界。"""

    if not isinstance(value, Mapping):
        return (_issue("subagent_contract", "manifest root must be an object"),)

    issues: list[SubagentManifestIssue] = []
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        issues.append(_issue("subagent_contract", "schema_version must be 1"))
    issues.extend(_validate_agent(value.get("agent"), directory_name=directory_name))
    issues.extend(_validate_template_config(value))
    issues.extend(_validate_session(value.get("session")))

    policy = value.get("workspace_policy")
    if not isinstance(policy, Mapping):
        issues.append(_issue("subagent_policy", "workspace_policy must be an object"))
        return tuple(issues)
    if policy.get("fail_closed") is not True:
        issues.append(
            _issue(
                "subagent_policy",
                "subagent workspace_policy must be fail_closed",
            ),
        )

    parsed, permission_issues = _validate_tool_policy(policy)
    issues.extend(permission_issues)
    allowed_tools = policy.get("allowed_tools")
    if isinstance(allowed_tools, list) and "TeamSay" not in allowed_tools:
        issues.append(
            _issue(
                "subagent_team_say_required",
                "unattended subagent allowed_tools must explicitly include TeamSay",
            ),
        )
    if parsed.get("ask_tools"):
        issues.append(
            _issue(
                "subagent_ask_unsupported",
                "unattended subagents cannot declare ask_tools",
            ),
        )
    return tuple(issues)


def _validate_template_config(
    value: Mapping[object, object],
) -> tuple[SubagentManifestIssue, ...]:
    issues: list[SubagentManifestIssue] = []
    for field, contract in (
        ("context_config", _ContextConfigContract),
        ("react_config", _ReActConfigContract),
    ):
        config = value.get(field)
        if not isinstance(config, Mapping):
            issues.append(_issue("subagent_contract", f"{field} must be an object"))
            continue
        try:
            contract.model_validate(config)
        except ValidationError:
            issues.append(
                _issue(
                    "subagent_contract",
                    f"{field} does not satisfy the AgentScope 2.0.8 schema",
                ),
            )
    invite = value.get("invite_config")
    if not isinstance(invite, Mapping) or dict(invite) != {"invitable": False}:
        issues.append(
            _issue(
                "subagent_contract",
                "invite_config must be exactly {invitable: false}",
            ),
        )
    return tuple(issues)


def _validate_agent(
    value: object,
    *,
    directory_name: str,
) -> tuple[SubagentManifestIssue, ...]:
    if not isinstance(value, Mapping):
        return (_issue("subagent_contract", "agent must be an object"),)
    issues: list[SubagentManifestIssue] = []
    agent_id = value.get("id")
    if not isinstance(agent_id, str) or _AGENT_ID.fullmatch(agent_id) is None or agent_id != directory_name:
        issues.append(
            _issue(
                "subagent_agent_id",
                "agent.id must be valid and match the subagent directory",
            ),
        )
    description = value.get("description")
    if not isinstance(description, str) or not description.strip():
        issues.append(_issue("subagent_contract", "agent.description is required"))
    if value.get("runtime") != "agentscope" or value.get("runtime_contract") != AGENTSCOPE_RUNTIME_CONTRACT:
        issues.append(
            _issue(
                "subagent_runtime",
                "AgentScope 2.0.8 public Runtime contract is required",
            ),
        )
    if value.get("system_prompt") != "AGENT.md":
        issues.append(
            _issue(
                "subagent_system_prompt",
                "agent.system_prompt must be AGENT.md",
            ),
        )
    return tuple(issues)


def _validate_session(value: object) -> tuple[SubagentManifestIssue, ...]:
    if not isinstance(value, Mapping):
        return (_issue("subagent_permission_mode", "session must be an object"),)
    if value.get("permission_mode") != WORKER_PERMISSION_MODE:
        return (
            _issue(
                "subagent_permission_mode",
                "unattended subagent permission_mode must be dont_ask",
            ),
        )
    return ()


def _validate_tool_policy(
    policy: Mapping[object, object],
) -> tuple[
    dict[str, set[tuple[str, str | None]]],
    tuple[SubagentManifestIssue, ...],
]:
    parsed: dict[str, set[tuple[str, str | None]]] = {}
    issues: list[SubagentManifestIssue] = []
    for field in _TOOL_POLICY_FIELDS:
        raw = policy.get(field, []) if field == "ask_tools" else policy.get(field)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            issues.append(_issue("invalid_tool_policy", f"{field} must be a string list"))
            parsed[field] = set()
            continue
        rules: list[tuple[str, str | None]] = []
        invalid = False
        for item in raw:
            if not item or item != item.strip() or "\0" in item:
                invalid = True
                continue
            name, separator, content = item.partition("(")
            if not name or (separator and (not item.endswith(")") or not content[:-1])):
                invalid = True
                continue
            if field in {"allowed_tools", "ask_tools"} and name.startswith("mcp__") and any(character in name for character in "*?["):
                issues.append(
                    _issue(
                        "wildcard_mcp_permission_forbidden",
                        f"{field} cannot wildcard MCP tools",
                    ),
                )
            rules.append((name, content[:-1] if separator else None))
        if invalid or len(rules) != len(set(rules)):
            issues.append(
                _issue(
                    "invalid_tool_policy",
                    f"{field} contains invalid or duplicate rules",
                ),
            )
        parsed[field] = set(rules)

    for index, left in enumerate(_TOOL_POLICY_FIELDS):
        for right in _TOOL_POLICY_FIELDS[index + 1 :]:
            if parsed[left] & parsed[right]:
                issues.append(
                    _issue(
                        "conflicting_tool_policy",
                        f"{left} and {right} contain the same rule",
                    ),
                )
    return parsed, tuple(issues)


def _issue(code: str, detail: str) -> SubagentManifestIssue:
    return SubagentManifestIssue(code=code, detail=detail)
