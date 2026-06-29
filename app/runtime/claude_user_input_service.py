from __future__ import annotations

import asyncio
import hashlib
import hmac
import posixpath
import secrets
import shlex
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
_REPORT_OUTPUT_ROOTS = ("/data/outputs", "/data/reports")
_READ_ONLY_MCP_PREFIXES = (
    "mcp__sec-ops-data__",
    "mcp__soc-playbook-query__",
    "mcp__soc-playbook-execution-result-query__",
)
_MCP_MUTATION_PARTS = (
    "write",
    "update",
    "delete",
    "block",
    "isolate",
    "disable",
    "kill",
    "quarantine",
    "execute",
    "create",
)
_BASH_ALLOWED_STDERR_REDIRECTS = {"2>/dev/null", "2>&1"}
_BASH_FALLBACK_COMMANDS = {"echo", "true"}
_BASH_READ_ONLY_COMMANDS = {"cat", "find", "grep", "head", "ls", "sed", "wc"}
_BASH_DANGEROUS_FIND_ARGS = {"-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprintf", "-ok", "-okdir"}
_BASH_SAFE_ABSOLUTE_READ_ROOTS = (
    "/data/business-agents/main-agent/workspace",
    "/data/outputs",
    "/data/reports",
)
_BASH_UNSAFE_RELATIVE_READ_PREFIXES = (".env", "secrets", "./.env", "./secrets")
_BASH_UNSAFE_COMMAND_MARKERS = ("\n", "\r", "`", "$(")


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
    business_agent_id: str
    run_id: str
    low_risk_category: str | None


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
        self._run_grants: set[tuple[str, str]] = set()

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
        low_risk_category = _low_risk_run_allow_category(tool_name, request_type, raw_input)
        if request_type == "tool_permission" and self._has_run_grant(business_agent_id, run_id):
            return SdkUserInputDecision(action="allow_once")
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
            risk_json=self._risk_payload(tool_name, request_type, low_risk_category),
            expires_at=expires_at,
        )
        pending = _PendingRuntimeRequest(
            request_id=request_id,
            future=loop.create_future(),
            event_queue=event_queue,
            raw_input=raw_input,
            request_type=request_type,
            tool_name=tool_name,
            business_agent_id=business_agent_id,
            run_id=run_id,
            low_risk_category=low_risk_category,
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
        if store_decision == "allow_for_run":
            self._remember_run_grant(pending)
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
        self.clear_run_grants(run_id)

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
            if decision.action == "allow_for_run":
                decision_data = {
                    "action": "allow_for_run",
                    "scope": "run",
                }
                if pending.low_risk_category:
                    decision_data["low_risk_category"] = pending.low_risk_category
                return SdkUserInputDecision(action="allow_once"), decision_data, "allow_for_run"
            if decision.action == "deny":
                message = (decision.message or "User denied Claude tool request.").strip()
                return SdkUserInputDecision(action="deny", message=message), {"message": message}, "deny"
            raise ClaudeUserInputInvalid("tool_permission requests only accept allow_once, allow_for_run, or deny")

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
    def _risk_payload(
        tool_name: str,
        request_type: str,
        low_risk_category: str | None,
    ) -> JsonObject:
        if request_type == "ask_user_question":
            return {
                "level": "info",
                "reason": "Claude needs additional user input.",
                "run_allow_eligible": False,
            }
        if low_risk_category:
            return {
                "level": "low",
                "reason": "Low-risk tool request can be allowed for the current run.",
                "run_allow_eligible": True,
                "run_allow_category": low_risk_category,
                "run_allow_scope": "run",
            }
        mutating = tool_name.startswith("mcp__") or tool_name in {"Bash", "Write", "Edit", "MultiEdit"}
        return {
            "level": "high" if mutating else "medium",
            "reason": "Tool execution requires user confirmation before Claude continues.",
            "run_allow_eligible": True,
            "run_allow_scope": "run",
        }

    def _has_run_grant(self, business_agent_id: str, run_id: str) -> bool:
        return (business_agent_id, run_id) in self._run_grants

    def _remember_run_grant(self, pending: _PendingRuntimeRequest) -> None:
        if pending.request_type == "tool_permission":
            self._run_grants.add((pending.business_agent_id, pending.run_id))

    def clear_run_grants(self, run_id: str) -> None:
        self._run_grants = {grant for grant in self._run_grants if grant[1] != run_id}


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _low_risk_run_allow_category(tool_name: str, request_type: str, input_data: JsonObject) -> str | None:
    if request_type != "tool_permission":
        return None
    if tool_name in {"Read", "Glob", "Grep", "Skill"}:
        return "read_only"
    if tool_name == "Write" and _is_report_output_path(_tool_input_path(input_data)):
        return "report_write"
    if tool_name == "Bash":
        return _bash_run_allow_category(input_data)
    if _is_read_only_mcp_tool(tool_name):
        return "mcp_read"
    return None


def _tool_input_path(input_data: JsonObject) -> str | None:
    for key in ("file_path", "filePath", "path"):
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_report_output_path(path: str | None) -> bool:
    normalized = _normalized_absolute_path(path)
    if normalized is None:
        return False
    return any(normalized == root or normalized.startswith(f"{root}/") for root in _REPORT_OUTPUT_ROOTS)


def _normalized_absolute_path(path: str | None) -> str | None:
    if not path or not path.startswith("/"):
        return None
    return posixpath.normpath(path)


def _bash_run_allow_category(input_data: JsonObject) -> str | None:
    command = input_data.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if any(marker in command for marker in _BASH_UNSAFE_COMMAND_MARKERS):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None
    executable = parts[0]
    args = parts[1:]
    if executable == "date" and all(arg == "-u" or arg == "--utc" or arg.startswith("+") for arg in args):
        return "bash_clock_read"
    if executable == "pwd" and all(arg in {"-L", "-P"} for arg in args):
        return "bash_workspace_read"
    if executable == "cat" and args and all(_is_template_path(arg) for arg in args):
        return "bash_template_read"
    if executable == "mkdir" and args[:1] == ["-p"] and args[1:] and all(_is_report_output_path(arg) for arg in args[1:]):
        return "bash_report_dir"
    if _is_safe_read_only_bash(parts):
        return "bash_read_only"
    return None


def _is_template_path(path: str) -> bool:
    normalized = posixpath.normpath(path)
    return normalized == "templates" or normalized.startswith("templates/")


def _is_read_only_mcp_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    if not any(lowered.startswith(prefix) for prefix in _READ_ONLY_MCP_PREFIXES):
        return False
    return not any(part in lowered for part in _MCP_MUTATION_PARTS)


def _is_safe_read_only_bash(parts: list[str]) -> bool:
    segments = _split_safe_bash_segments(parts)
    if segments is None:
        return False
    for index, segment in enumerate(segments):
        if segment == ["|"] or segment == ["||"]:
            continue
        is_fallback = index > 0 and segments[index - 1] == ["||"]
        if is_fallback:
            if not _is_safe_fallback_segment(segment):
                return False
            continue
        if not _is_read_only_segment(segment):
            return False
    return True


def _split_safe_bash_segments(parts: list[str]) -> list[list[str]] | None:
    segments: list[list[str]] = [[]]
    saw_fallback = False
    for token in parts:
        if token in {"|", "||"}:
            if not segments[-1] or (token == "|" and saw_fallback):
                return None
            if token == "||":
                if saw_fallback:
                    return None
                saw_fallback = True
            segments.append([token])
            segments.append([])
            continue
        if not _is_safe_bash_token(token):
            return None
        if token not in _BASH_ALLOWED_STDERR_REDIRECTS:
            segments[-1].append(token)
    return segments if segments and segments[-1] else None


def _is_safe_bash_token(token: str) -> bool:
    if not token or "\n" in token or "\r" in token:
        return False
    if token in _BASH_ALLOWED_STDERR_REDIRECTS:
        return True
    if token in {"&&", "&", ";", ">", "<"}:
        return False
    if any(marker in token for marker in ("`", "$(", ";", "<", ">")):
        return False
    return "|" not in token


def _is_safe_fallback_segment(segment: list[str]) -> bool:
    return bool(segment) and segment[0] in _BASH_FALLBACK_COMMANDS


def _is_read_only_segment(segment: list[str]) -> bool:
    if not segment:
        return False
    executable, *args = segment
    if executable not in _BASH_READ_ONLY_COMMANDS:
        return False
    if executable == "find" and any(arg in _BASH_DANGEROUS_FIND_ARGS for arg in args):
        return False
    if executable == "sed" and any(arg == "-i" or arg.startswith("--in-place") for arg in args):
        return False
    return all(_is_safe_read_only_arg(arg) for arg in args)


def _is_safe_read_only_arg(arg: str) -> bool:
    if arg.startswith("-") or arg.isdigit():
        return True
    if arg in {"f", "d", "l"}:
        return True
    if _is_path_like_token(arg):
        return _is_safe_read_path(arg)
    return True


def _is_path_like_token(value: str) -> bool:
    return value in {".", ".."} or value.startswith(("/", "./", "../", "~")) or "/" in value


def _is_safe_read_path(path: str) -> bool:
    if path.startswith("~"):
        return False
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../"):
        return False
    if path.startswith("/"):
        for root in _BASH_SAFE_ABSOLUTE_READ_ROOTS:
            if normalized != root and not normalized.startswith(f"{root}/"):
                continue
            if root.endswith("/workspace"):
                relative = normalized.removeprefix(root).lstrip("/")
                return _is_safe_relative_read_path(relative)
            return True
        return False
    relative = normalized[2:] if normalized.startswith("./") else normalized
    return _is_safe_relative_read_path(relative)


def _is_safe_relative_read_path(relative: str) -> bool:
    return not any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in _BASH_UNSAFE_RELATIVE_READ_PREFIXES)
