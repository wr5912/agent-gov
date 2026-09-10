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
                    "name": "SOC console investigation",
                },
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
                    "client_operation_id": "soc-console-turn-20260909-001",
                    "input": {
                        "name": "user",
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "请核查当前告警并给出处置建议",
                            }
                        ],
                    },
                    "confirmation_scope": "once",
                    "alert_id": "alert-20260909-001",
                    "case_id": "case-20260909-001",
                    "metadata": {"source": "soc-console"},
                },
            ),
            "resume_user_confirmation": example(
                "Resume the same run with one native confirmation result",
                {
                    "agent_id": _RUNTIME_AGENT_ID,
                    "session_id": "session-id-from-create",
                    "client_operation_id": "soc-console-turn-20260909-001",
                    "expected_run_id": "run-id-from-initial-turn",
                    "input": {
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
                    "confirmation_scope": "once",
                    "metadata": {"source": "soc-console"},
                },
                description=(
                    "The browser must not submit permission rules. Use confirmation_scope=run only after an "
                    "explicit user choice; AgentGov derives any run-scoped rule from the persisted tool call."
                ),
            ),
        },
    ),
}
