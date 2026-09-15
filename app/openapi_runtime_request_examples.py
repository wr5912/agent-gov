"""AgentScope Runtime 公共入口的请求示例。"""

from __future__ import annotations

from collections.abc import Mapping

from app.openapi_example_contracts import OperationKey, RequestExampleContract, example

_RUNTIME_AGENT_ID = "runtime-agent-version-20260909-001"

RUNTIME_REQUEST_EXAMPLE_CONTRACTS: Mapping[OperationKey, RequestExampleContract] = {
    ("/api/runtime/sessions/", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description=(
            "Create an AgentScope session pinned to the currently published immutable Harness version. "
            "Send a stable Idempotency-Key header so a transport retry cannot create a second session."
        ),
        examples={
            "create_published_agent_session": example(
                "Create a session for the published business Agent",
                {
                    "agent_id": _RUNTIME_AGENT_ID,
                    "name": "文档助手会话",
                },
            )
        },
    ),
    ("/api/runtime/sessions/{session_id}", "patch"): RequestExampleContract(
        media_type="application/json",
        operation_description=(
            "Rename the owned AgentScope session. Only name is accepted; Runtime settings, permissions, "
            "credentials, and workspace fields remain governed by the published session binding."
        ),
        examples={
            "rename_session": example(
                "Rename one existing session",
                {"name": "文档助手后续问答"},
            )
        },
    ),
    ("/api/runtime/chat/", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description=(
            "Start a governed AgentScope turn, or resume the same waiting run with a native HITL result. "
            "A normal turn creates one AgentGov run; USER_CONFIRM_RESULT and EXTERNAL_EXECUTION_RESULT "
            "must carry the waiting reply_id and resume that existing run."
        ),
        examples={
            "agent_scope_message": example(
                "Start one governed AgentScope turn",
                {
                    "agent_id": _RUNTIME_AGENT_ID,
                    "session_id": "session-id-from-create",
                    "input": {
                        "id": "user-input-20260913-001",
                        "name": "user",
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "请阅读项目说明并总结部署步骤",
                            }
                        ],
                    },
                },
            ),
            "resume_user_confirmation": example(
                "Resume the same run with one native confirmation result",
                {
                    "agent_id": _RUNTIME_AGENT_ID,
                    "session_id": "session-id-from-create",
                    "input": {
                        "id": "confirmation-20260913-001",
                        "type": "USER_CONFIRM_RESULT",
                        "reply_id": "reply-id-from-require-user-confirm",
                        "confirm_results": [
                            {
                                "confirmed": True,
                                "tool_call": {
                                    "type": "tool_call",
                                    "id": "tool-call-id-from-event",
                                    "name": "Read",
                                    "input": '{"path":"AGENT.md"}',
                                },
                            }
                        ],
                    },
                },
                description=(
                    "仅在用户明确选择本次运行允许时发送 X-AgentGov-Confirmation-Scope: run 请求头；"
                    "单次允许/拒绝不需要该头，不能提交自定义权限规则。同一次动作重试必须复用 input.id。"
                ),
            ),
        },
    ),
}
