"""AgentGov 与固定 AgentScope Runtime 共享的 Harness 和 Session Workspace 契约。"""

from __future__ import annotations

import json
import re
import uuid

AGENTSCOPE_RUNTIME_CONTRACT = "agentscope-app/2.0.8"
RUNTIME_TEMPLATE_RESTART_REQUIRED = "RUNTIME_TEMPLATE_RESTART_REQUIRED"

_SESSION_WORKSPACE_SEPARATOR = "--s-"
_SESSION_WORKSPACE_TOKEN = re.compile(
    r"session-intent-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)


class RuntimeTemplateRestartRequired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("AgentScope Runtime must restart to load the prepared subagent templates")


def is_runtime_template_restart_response(status_code: int, body: bytes) -> bool:
    if status_code != 409:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("error_code") == RUNTIME_TEMPLATE_RESTART_REQUIRED


def session_creation_token(identity: uuid.UUID | str) -> str:
    """调用方保留 UUID 的随机或幂等语义，共享边界只固定传输格式。"""
    return f"session-intent-{uuid.UUID(str(identity))}"


def session_workspace_id(version_binding: str, identity: uuid.UUID | str) -> str:
    return f"{version_binding}{_SESSION_WORKSPACE_SEPARATOR}{session_creation_token(identity)}"


def version_workspace_id(workspace_id: str) -> str:
    """取回版本绑定，同时拒绝非法 Session token；无后缀的版本绑定仍合法。"""
    if _SESSION_WORKSPACE_SEPARATOR not in workspace_id:
        return workspace_id
    version_binding, token = workspace_id.rsplit(_SESSION_WORKSPACE_SEPARATOR, 1)
    if _SESSION_WORKSPACE_TOKEN.fullmatch(token) is None:
        raise ValueError("workspace_id contains an invalid Session creation token")
    return version_binding
