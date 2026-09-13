"""Runtime 写操作的稳定身份与无正文请求指纹。"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any


class RuntimeChatOperationKind(StrEnum):
    INITIAL = "initial"
    USER_CONFIRMATION = "user_confirmation"
    EXTERNAL_EXECUTION = "external_execution"


LEGACY_UNKNOWN_SESSION_REQUEST_FINGERPRINT = "legacy-v2-session-name-unavailable"


def canonical_request_fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def session_creation_request_fingerprint(
    runtime_agent_id: str,
    requested_name: str | None,
) -> str:
    return canonical_request_fingerprint(
        {
            "runtime_agent_id": runtime_agent_id,
            "name": requested_name,
        },
    )


def initial_operation_key(client_operation_id: str) -> str:
    return f"initial:{client_operation_id}"


def continuation_operation_key(
    *,
    client_operation_id: str,
    operation_kind: RuntimeChatOperationKind,
    action_session_id: str,
    reply_id: str,
    action_ids: list[str],
    tool_call_ids: list[str],
) -> str:
    identity_sha256 = canonical_request_fingerprint(
        {
            "operation_kind": operation_kind.value,
            "action_session_id": action_session_id,
            "reply_id": reply_id,
            "action_ids": sorted(action_ids),
            "tool_call_ids": sorted(tool_call_ids),
        },
    )
    return f"continuation:{client_operation_id}:{identity_sha256}"


def chat_request_fingerprint(
    *,
    input_value: Any,
    metadata: dict[str, object],
    session_id: str,
    runtime_agent_id: str,
    alert_id: str | None,
    case_id: str | None,
    expected_run_id: str | None,
    confirmation_scope: str | None,
) -> str:
    return canonical_request_fingerprint(
        {
            "input": input_value,
            "metadata": metadata,
            "session_id": session_id,
            "runtime_agent_id": runtime_agent_id,
            "alert_id": alert_id,
            "case_id": case_id,
            "expected_run_id": expected_run_id,
            "confirmation_scope": confirmation_scope,
        },
    )
