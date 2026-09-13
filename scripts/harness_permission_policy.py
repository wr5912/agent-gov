"""Claude 权限声明到 AgentScope Harness 规则的纯转换与校验。"""

from __future__ import annotations

from typing import Any

from harness_conversion_io import rewrite_text, string_list


def convert_permission_rules(value: Any) -> list[str]:
    """转换旧路径，并删除只属于 Claude Runtime 的规则。"""

    converted: list[str] = []
    for rule in string_list(value):
        if "claude-root" in rule or ".claude/projects" in rule or "./hooks/**" in rule:
            continue
        rewritten = rewrite_text(rule)
        if "skills/**" in rewritten and rewritten.startswith(("Edit", "Write", "NotebookEdit")):
            converted.extend(
                [
                    rewritten,
                    rewritten.replace("skills/**", "subagents/**"),
                ],
            )
        else:
            converted.append(rewritten)
    return sorted(dict.fromkeys(converted))


def validate_converted_permission_rules(
    *,
    allowed_tools: list[str],
    ask_tools: list[str],
    denied_tools: list[str],
) -> None:
    """拒绝畸形规则、MCP 通配放行及跨行为的相同规则。"""

    groups = {
        "allowed_tools": allowed_tools,
        "ask_tools": ask_tools,
        "denied_tools": denied_tools,
    }
    for field, rules in groups.items():
        if any(not isinstance(rule, str) or not rule or rule != rule.strip() or "\0" in rule for rule in rules):
            raise ValueError(f"converted workspace_policy.{field} contains an invalid rule")
        if len(rules) != len(set(rules)):
            raise ValueError(f"converted workspace_policy.{field} contains duplicate rules")
        for rule in rules:
            name, separator, content = rule.partition("(")
            if not name or (separator and (not rule.endswith(")") or not content[:-1])):
                raise ValueError(f"converted workspace_policy.{field} contains an invalid rule")
            if field in {"allowed_tools", "ask_tools"} and name.startswith("mcp__") and any(character in name for character in "*?["):
                raise ValueError(f"converted workspace_policy.{field} cannot wildcard MCP tools")
    identities = {field: set(rules) for field, rules in groups.items()}
    fields = tuple(groups)
    for index, left in enumerate(fields):
        for right in fields[index + 1 :]:
            if identities[left] & identities[right]:
                raise ValueError(f"converted workspace policy conflicts between {left} and {right}")
