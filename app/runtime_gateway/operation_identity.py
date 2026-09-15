"""Runtime 写操作的稳定身份与无正文请求指纹。"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum


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
