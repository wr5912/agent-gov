from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from app.runtime.json_types import JsonObject

ClaudeUserInputRequestType = Literal["tool_permission", "ask_user_question"]
ClaudeUserInputStatus = Literal["waiting", "resolved", "cancelled"]
ClaudeUserInputDecision = Literal[
    "allow_once",
    "allow_for_run",
    "deny",
    "answer_question",
    "timeout_deny",
    "client_cancelled",
    "service_restarted",
    "runtime_interrupted",
]

REQUEST_TYPES: set[str] = {"tool_permission", "ask_user_question"}
STATUSES: set[str] = {"waiting", "resolved", "cancelled"}
DECISIONS: set[str] = {
    "allow_once",
    "allow_for_run",
    "deny",
    "answer_question",
    "timeout_deny",
    "client_cancelled",
    "service_restarted",
    "runtime_interrupted",
}

TERMINAL_STATUS_BY_DECISION: dict[str, str] = {
    "allow_once": "resolved",
    "allow_for_run": "resolved",
    "deny": "resolved",
    "answer_question": "resolved",
    "timeout_deny": "resolved",
    "client_cancelled": "cancelled",
    "service_restarted": "cancelled",
    "runtime_interrupted": "cancelled",
}


def terminal_status_for_decision(decision: str) -> str:
    if decision not in TERMINAL_STATUS_BY_DECISION:
        raise ValueError(f"Unknown Claude user input decision: {decision}")
    return TERMINAL_STATUS_BY_DECISION[decision]


def ensure_transition(current_status: str, next_status: str) -> None:
    if current_status != "waiting":
        raise ValueError(f"Claude user input request is already terminal: {current_status}")
    if next_status not in {"resolved", "cancelled"}:
        raise ValueError(f"Invalid Claude user input terminal status: {next_status}")


@dataclass(frozen=True)
class ClaudeUserInputRequestRecord:
    request_id: str
    business_agent_id: str
    run_id: str
    api_session_id: str
    request_type: ClaudeUserInputRequestType
    tool_name: str
    status: ClaudeUserInputStatus
    created_at: str
    expires_at: str
    decision_token_hash: str
    sdk_session_id: Optional[str] = None
    tool_use_id: Optional[str] = None
    sdk_subagent_id: Optional[str] = None
    input_json: JsonObject = field(default_factory=dict)
    context_json: JsonObject = field(default_factory=dict)
    risk_json: JsonObject = field(default_factory=dict)
    decision: Optional[ClaudeUserInputDecision] = None
    decision_payload_json: JsonObject = field(default_factory=dict)
    decided_by: Optional[str] = None
    resolved_at: Optional[str] = None

    def public_payload(self, *, include_token: str | None = None) -> JsonObject:
        response_data = {
            "request_id": self.request_id,
            "business_agent_id": self.business_agent_id,
            "run_id": self.run_id,
            "session_id": self.api_session_id,
            "api_session_id": self.api_session_id,
            "sdk_session_id": self.sdk_session_id,
            "tool_use_id": self.tool_use_id,
            "sdk_subagent_id": self.sdk_subagent_id,
            "request_type": self.request_type,
            "tool_name": self.tool_name,
            "input": self.input_json,
            "context": self.context_json,
            "risk": self.risk_json,
            "status": self.status,
            "decision": self.decision,
            "decision_payload": self.decision_payload_json,
            "decided_by": self.decided_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "resolved_at": self.resolved_at,
        }
        if include_token is not None:
            response_data["decision_token"] = include_token
        return response_data
