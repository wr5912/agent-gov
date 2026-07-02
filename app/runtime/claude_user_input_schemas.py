from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.json_types import JsonObject


class ClaudeUserInputRequestResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    request_id: str
    business_agent_id: str
    run_id: str
    session_id: str
    api_session_id: str
    sdk_session_id: Optional[str] = None
    tool_use_id: Optional[str] = None
    sdk_subagent_id: Optional[str] = None
    request_type: Literal["tool_permission", "ask_user_question"]
    tool_name: str
    input: JsonObject = Field(default_factory=dict)
    context: JsonObject = Field(default_factory=dict)
    risk: JsonObject = Field(default_factory=dict)
    status: Literal["waiting", "resolved", "cancelled"]
    decision: Optional[str] = None
    decision_data: JsonObject = Field(default_factory=dict, alias="decision_payload")
    decided_by: Optional[str] = None
    created_at: str
    expires_at: str
    resolved_at: Optional[str] = None


class ClaudeUserInputRequestListResponse(BaseModel):
    requests: list[ClaudeUserInputRequestResponse]


class ClaudeUserInputDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["allow_once", "allow_for_run", "deny", "answer_question"]
    decision_token: str
    run_id: str
    session_id: str
    business_agent_id: str
    answers: JsonObject = Field(default_factory=dict)
    response: Optional[str] = None
    message: Optional[str] = None


class ClaudeUserInputDecisionResponse(BaseModel):
    request_id: str
    status: Literal["resolved", "cancelled"]
    decision: str
    resolved_at: Optional[str] = None
