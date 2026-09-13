"""AgentGov run 级工具授权的跨进程安全契约。"""

from __future__ import annotations

RUN_SCOPED_PATH_TOOLS = frozenset({"Read", "Write", "Edit"})


def is_bounded_run_path_rule(tool_name: str, rule_content: str | None) -> bool:
    """Run scope 只允许语义稳定的 file_path 规则。

    AgentScope 的 Bash 规则使用子串/通配匹配，Glob 又可同时匹配 pattern
    与 path，都不能从字符串外观推导单一资源范围。Read/Write/Edit
    共享 file_path + fnmatch 契约，标准建议为 ``<parent>/**``，因此只
    允许精确路径或具有固定前缀的该形式。
    """

    if tool_name not in RUN_SCOPED_PATH_TOOLS or not isinstance(rule_content, str):
        return False
    pattern = rule_content.strip()
    if not pattern or pattern != rule_content or "\0" in pattern or len(pattern) > 1024:
        return False
    if any(character in pattern for character in "?[]\\"):
        return False
    if "*" in pattern:
        if not pattern.endswith("/**") or "*" in pattern[:-3]:
            return False
        fixed_prefix = pattern[:-3]
    else:
        fixed_prefix = pattern
    if "//" in fixed_prefix or fixed_prefix in {"", ".", "./", "/", "~"}:
        return False
    parts = [part for part in fixed_prefix.removeprefix("./").split("/") if part]
    return bool(parts) and all(part not in {".", "..", "~"} for part in parts)
