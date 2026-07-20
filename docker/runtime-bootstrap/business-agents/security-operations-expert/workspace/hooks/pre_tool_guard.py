#!/usr/bin/env python3
"""Claude Code PreToolUse hook: hard-deny unsafe and governance-plane writes.

This hook does not replace Claude Code authorization. Settings route SOC
mutations away from the read-only Agent and hard-deny unsafe direct commands.
This hook never returns allow; it only denies commands that must never run and
any attempt to modify the Agent governance surface.
"""

import json
import re
import sys


def deny(reason: str) -> None:
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


try:
    payload = json.load(sys.stdin)
except Exception:
    deny("PreToolUse 守卫无法解析工具输入，安全起见已阻止。")

tool_name = payload.get("tool_name", "")
tool_input = payload.get("tool_input", {})
if not isinstance(tool_input, dict):
    deny("PreToolUse 守卫收到非法工具参数，安全起见已阻止。")

command = tool_input.get("command", "")
command = command if isinstance(command, str) else ""

DENY_PATTERNS = (
    r"rm\s+-rf\s+/(\s|$)",
    r"mkfs\.",
    r"dd\s+if=.*\s+of=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"curl\s+[^|]+\|\s*(sh|bash)",
    r"wget\s+[^|]+\|\s*(sh|bash)",
)
RISKY_PRODUCTION_PATTERNS = (
    r"\biptables\b.*\s-F\b",
    r"\bkubectl\b\s+delete\b",
    r"\bterraform\b\s+apply\b",
    r"\bansible-playbook\b.*(--limit\s+all|production|prod)",
    r"\bsystemctl\b\s+(restart|stop)\b",
    r"\b(nmap|masscan)\b.*(-sS|-sT|-A|--script)",
)
GOVERNANCE_PATH_PATTERNS = (
    r"(^|/|\s)\.mcp\.json($|/|\s)",
    r"(^|/|\s)CLAUDE\.md($|/|\s)",
    r"(^|/|\s)agent\.yaml($|/|\s)",
    r"(^|/|\s)\.claude(/|\s|$)",
    r"(^|/|\s)hooks(/|\s|$)",
)
BASH_MUTATION_PATTERN = re.compile(
    r"(^|[;&|]\s*)(rm|mv|cp|install|chmod|chown|truncate|tee|sed\s+-i|perl\s+-pi)\b|>>?|\bopen\s*\(",
    flags=re.IGNORECASE,
)


def is_governance_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in GOVERNANCE_PATH_PATTERNS)


if tool_name in {"Edit", "Write", "NotebookEdit"}:
    candidate_paths = [
        tool_input.get("file_path"),
        tool_input.get("path"),
        tool_input.get("notebook_path"),
    ]
    if any(isinstance(path, str) and is_governance_path(path) for path in candidate_paths):
        deny("Agent 治理文件由 AgentGov seed 管理，禁止在会话中修改。")

if command and is_governance_path(command) and BASH_MUTATION_PATTERN.search(command):
    deny("检测到通过 Bash 修改 Agent 治理文件的尝试，已阻止。")

for pattern in DENY_PATTERNS:
    if command and re.search(pattern, command, flags=re.IGNORECASE):
        deny("检测到高危破坏性命令，已阻止。请改为生成处置计划或 dry-run。")

for pattern in RISKY_PRODUCTION_PATTERNS:
    if command and re.search(pattern, command, flags=re.IGNORECASE):
        deny(
            "该命令可能影响生产环境，已阻止 Agent 直接执行。请改为输出处置计划"
            "（含审批、影响范围、回滚方案、验证方法）或由人工执行。"
        )

# No decision means continue with normal permission flow. This hook never allows.
raise SystemExit(0)
