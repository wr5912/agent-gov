from __future__ import annotations

SECURITY_OPERATIONS_EXPERT_AGENT_ID = "security-operations-expert"
SECURITY_OPERATIONS_EXECUTE_TOOL = "mcp__sec-ops__soc_api__execute"
ASK_USER_QUESTION_TOOL = "AskUserQuestion"


def blocks_interactive_question(profile_name: str, tool_name: str) -> bool:
    """security-operations-expert is backend-driven; it must not pause on ad-hoc questions."""
    return profile_name == SECURITY_OPERATIONS_EXPERT_AGENT_ID and tool_name == ASK_USER_QUESTION_TOOL


def tool_requires_web_hitl(profile_name: str, tool_name: str) -> bool:
    """Return whether a can_use_tool callback should surface Web HITL for a tool."""
    if profile_name == SECURITY_OPERATIONS_EXPERT_AGENT_ID:
        return tool_name == SECURITY_OPERATIONS_EXECUTE_TOOL
    return True
