from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic.types import JsonValue

from app.runtime.claude_user_input_schemas import ClaudeUserInputDecisionRequest
from app.runtime.json_types import JsonObject
from app.runtime.message_utils import to_plain
from app.runtime.records.claude_user_input_records import ClaudeUserInputRequestRecord
from app.runtime.stores.claude_user_input_store import ClaudeUserInputStore

_DEFAULT_TIMEOUT_SECONDS = 300
_SENSITIVE_KEY_PARTS = ("authorization", "token", "secret", "password", "api_key", "apikey", "key", "credential")
_MAX_STRING_LENGTH = 600
_MAX_LIST_ITEMS = 50


class ClaudeUserInputError(Exception):
    """Base class for user-input decision errors."""


class ClaudeUserInputNotFound(ClaudeUserInputError):
    pass


class ClaudeUserInputConflict(ClaudeUserInputError):
    pass


class ClaudeUserInputInvalid(ClaudeUserInputError):
    pass


@dataclass(frozen=True)
class SdkUserInputDecision:
    action: str
    message: str = ""
    ask_user_question_input: JsonObject | None = None


@dataclass
class _PendingRuntimeRequest:
    request_id: str
    future: asyncio.Future[SdkUserInputDecision]
    event_queue: asyncio.Queue[JsonObject | None]
    raw_input: JsonObject
    request_type: str
    tool_name: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def redact_user_input(value: object) -> JsonValue:
    if isinstance(value, dict):
        redacted: JsonObject = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SENSITIVE_KEY_PARTS):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = redact_user_input(item)
        return redacted
    if isinstance(value, (list, tuple)):
        items = [redact_user_input(item) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append({"truncated_items": len(value) - _MAX_LIST_ITEMS})
        return items
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_LENGTH:
            return value
        return value[:_MAX_STRING_LENGTH] + f"...<truncated {len(value) - _MAX_STRING_LENGTH} chars>"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def json_object(value: Any) -> JsonObject:
    plain = to_plain(value)
    return plain if isinstance(plain, dict) else {"value": plain}


class ClaudeUserInputService:
    def __init__(self, store: ClaudeUserInputStore, *, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, _PendingRuntimeRequest] = {}

    def cancel_orphan_waiting_requests(self, *, reason: str = "service_restarted") -> list[ClaudeUserInputRequestRecord]:
        return self._store.cancel_waiting_requests(decision=reason, decided_by="system")

    async def create_and_wait(
        self,
        *,
        event_queue: asyncio.Queue[JsonObject | None],
        business_agent_id: str,
        run_id: str,
        api_session_id: str,
        sdk_session_id: Optional[str],
        tool_name: str,
        input_data: Any,
        context: Any,
    ) -> SdkUserInputDecision:
        request_type = "ask_user_question" if tool_name == "AskUserQuestion" else "tool_permission"
        request_id = f"cur-{uuid.uuid4()}"
        token = secrets.token_urlsafe(32)
        loop = asyncio.get_running_loop()
        raw_input = json_object(input_data)
        context_json = json_object(context)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self._timeout_seconds)).isoformat()
        record = self._store.create(
            request_id=request_id,
            decision_token_hash=_token_hash(token),
            business_agent_id=business_agent_id,
            run_id=run_id,
            api_session_id=api_session_id,
            sdk_session_id=sdk_session_id,
            tool_use_id=_optional_str(context_json.get("tool_use_id")),
            sdk_subagent_id=_optional_str(context_json.get("agent_id")),
            request_type=request_type,
            tool_name=tool_name,
            redacted_input_json=json_object(redact_user_input(raw_input)),
            context_json=json_object(redact_user_input(context_json)),
            risk_json=self._risk_payload(tool_name, request_type),
            expires_at=expires_at,
        )
        pending = _PendingRuntimeRequest(
            request_id=request_id,
            future=loop.create_future(),
            event_queue=event_queue,
            raw_input=raw_input,
            request_type=request_type,
            tool_name=tool_name,
        )
        self._pending[request_id] = pending
        await event_queue.put({"event": "claude_user_input_required", "data": record.public_payload(include_token=token)})
        try:
            return await asyncio.wait_for(pending.future, timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            record = self._store.finish(
                request_id,
                decision="timeout_deny",
                decision_payload_json={"message": "Claude tool request timed out waiting for user confirmation."},
                decided_by="system",
            )
            await event_queue.put({"event": "claude_user_input_resolved", "data": record.public_payload()})
            return SdkUserInputDecision(action="deny", message="Timed out waiting for user confirmation.")
        finally:
            self._pending.pop(request_id, None)

    def submit_decision(
        self,
        request_id: str,
        *,
        decision: ClaudeUserInputDecisionRequest,
        decided_by: str,
    ) -> ClaudeUserInputRequestRecord:
        record = self._store.get(request_id)
        if record is None:
            raise ClaudeUserInputNotFound(f"Claude user input request not found: {request_id}")
        if record.status != "waiting":
            raise ClaudeUserInputConflict(f"Claude user input request is already {record.status}")
        if (
            decision.run_id != record.run_id
            or decision.session_id != record.api_session_id
            or decision.business_agent_id != record.business_agent_id
        ):
            raise ClaudeUserInputConflict("Claude user input decision context does not match the waiting request")
        if not hmac.compare_digest(record.decision_token_hash, _token_hash(decision.decision_token)):
            raise ClaudeUserInputConflict("Claude user input decision token is invalid")
        pending = self._pending.get(request_id)
        if pending is None:
            raise ClaudeUserInputConflict("Claude execution is no longer waiting for this request")
        sdk_decision, decision_data, store_decision = self._build_sdk_decision(pending, decision)
        updated = self._store.finish(
            request_id,
            decision=store_decision,
            decision_payload_json=decision_data,
            decided_by=decided_by,
        )
        if not pending.future.done():
            pending.future.set_result(sdk_decision)
        pending.event_queue.put_nowait({"event": "claude_user_input_resolved", "data": updated.public_payload()})
        return updated

    async def cancel_run(self, run_id: str, *, decision: str = "client_cancelled") -> None:
        request_ids: list[str] = []
        for request_id in self._pending:
            record = self._store.get(request_id)
            if record and record.run_id == run_id:
                request_ids.append(request_id)
        if not request_ids:
            return
        records = self._store.cancel_waiting_requests(decision=decision, decided_by="system", only_request_ids=request_ids)
        records_by_id = {record.request_id: record for record in records}
        for request_id in request_ids:
            pending = self._pending.pop(request_id, None)
            if pending and not pending.future.done():
                pending.future.set_result(SdkUserInputDecision(action="deny", message="Client disconnected before confirmation."))
            if pending and request_id in records_by_id:
                pending.event_queue.put_nowait({"event": "claude_user_input_resolved", "data": records_by_id[request_id].public_payload()})

    def list_requests(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        business_agent_id: str | None = None,
        limit: int = 100,
    ) -> list[ClaudeUserInputRequestRecord]:
        return self._store.list(
            session_id=session_id,
            run_id=run_id,
            status=status,
            business_agent_id=business_agent_id,
            limit=limit,
        )

    @staticmethod
    def _build_sdk_decision(
        pending: _PendingRuntimeRequest,
        decision: ClaudeUserInputDecisionRequest,
    ) -> tuple[SdkUserInputDecision, JsonObject, str]:
        if pending.request_type == "tool_permission":
            if decision.action == "allow_once":
                return SdkUserInputDecision(action="allow_once"), {"action": "allow_once"}, "allow_once"
            if decision.action == "deny":
                message = (decision.message or "User denied Claude tool request.").strip()
                return SdkUserInputDecision(action="deny", message=message), {"message": message}, "deny"
            raise ClaudeUserInputInvalid("tool_permission requests only accept allow_once or deny")

        if decision.action != "answer_question":
            raise ClaudeUserInputInvalid("ask_user_question requests require answer_question")
        if not decision.answers and not (decision.response and decision.response.strip()):
            raise ClaudeUserInputInvalid("answer_question requires answers or response")
        updated_input = dict(pending.raw_input)
        if decision.answers:
            updated_input["answers"] = decision.answers
        if decision.response and decision.response.strip():
            updated_input["response"] = decision.response.strip()
        decision_data: JsonObject = {}
        if decision.answers:
            decision_data["answers"] = decision.answers
        if decision.response and decision.response.strip():
            decision_data["response"] = decision.response.strip()
        return SdkUserInputDecision(action="answer_question", ask_user_question_input=updated_input), decision_data, "answer_question"

    @staticmethod
    def _risk_payload(tool_name: str, request_type: str) -> JsonObject:
        if request_type == "ask_user_question":
            return {"level": "info", "reason": "Claude needs additional user input."}
        mutating = tool_name.startswith("mcp__") or tool_name in {"Bash", "Write", "Edit", "MultiEdit"}
        return {
            "level": "high" if mutating else "medium",
            "reason": "Tool execution requires user confirmation before Claude continues.",
        }


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
