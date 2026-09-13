"""AgentScope HITL continuation validation and run-scoped rule projection."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import utc_now

from .contracts import (
    ConfirmationScope,
    RuntimeReceipt,
    RuntimeToolCallFingerprint,
    governed_run_permission_rules,
)
from .models import AgentRunModel, RuntimePendingActionModel

_HITL_RECEIPT_KINDS = {
    "REQUIRE_USER_CONFIRM": "human",
    "REQUIRE_EXTERNAL_EXECUTION": "external",
}


class HITLValidationError(ValueError):
    """The continuation differs from the exact persisted pending action."""


def tool_call_fingerprint(
    value: object,
    *,
    default_state: str | None = None,
) -> RuntimeToolCallFingerprint:
    """Hash one complete canonical ToolCall without retaining any body field."""

    if not isinstance(value, dict):
        raise HITLValidationError("HITL tool call must be an object")
    tool_call_id = value.get("id")
    tool_call_name = value.get("name")
    tool_call_state = value.get("state", default_state)
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise HITLValidationError("HITL tool call is missing its stable id")
    if not isinstance(tool_call_name, str) or not tool_call_name:
        raise HITLValidationError("HITL tool call is missing its stable name")
    canonical = _canonical_tool_call_bytes(value)
    try:
        return RuntimeToolCallFingerprint(
            tool_call_id=tool_call_id,
            tool_call_name=tool_call_name,
            tool_call_state=tool_call_state,
            tool_call_utf8_length=len(canonical),
            tool_call_sha256=hashlib.sha256(canonical).hexdigest(),
        )
    except ValidationError as exc:
        raise HITLValidationError("HITL tool call state is invalid") from exc


def parse_tool_call_fingerprint(
    value: object,
    *,
    expected_id: str | None = None,
    expected_name: str | None = None,
) -> RuntimeToolCallFingerprint:
    """Validate one current fingerprint without accepting legacy raw ToolCall rows."""

    try:
        fingerprint = RuntimeToolCallFingerprint.model_validate(value)
    except ValidationError as exc:
        raise HITLValidationError(
            "HITL tool call must use the fingerprint contract",
        ) from exc
    if expected_id is not None and fingerprint.tool_call_id != expected_id:
        raise HITLValidationError("Persisted HITL tool call id does not match its ledger identity")
    if expected_name is not None and fingerprint.tool_call_name != expected_name:
        raise HITLValidationError("Persisted HITL tool name does not match its ledger identity")
    return fingerprint


def validate_runtime_receipt_fingerprints(receipt: RuntimeReceipt) -> RuntimeReceipt:
    """Require canonical fingerprint-only HITL payloads at the online boundary."""

    if receipt.type not in _HITL_RECEIPT_KINDS:
        return receipt
    return receipt.model_copy(
        update={
            "payload": _validated_hitl_payload(
                receipt.type,
                receipt.payload,
            ),
        },
    )


def _validated_hitl_payload(event_type: str, payload: object) -> JsonObject:
    if not isinstance(payload, dict):
        raise HITLValidationError("HITL receipt payload must be an object")
    if set(payload) != {"tool_calls"}:
        raise HITLValidationError(
            "HITL receipt payload must contain only fingerprinted tool_calls",
        )
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise HITLValidationError("HITL receipt is missing tool_calls")
    if event_type not in _HITL_RECEIPT_KINDS:  # pragma: no cover - caller guards
        raise HITLValidationError("HITL receipt type is invalid")
    return {
        "tool_calls": [parse_tool_call_fingerprint(tool_call).model_dump(mode="json") for tool_call in tool_calls],
    }


def _default_tool_call_state(kind: str) -> str:
    if kind == "human":
        return "asking"
    if kind == "external":
        return "pending"
    raise HITLValidationError("Pending action kind is invalid")


def _canonical_tool_call_bytes(value: JsonObject) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HITLValidationError("HITL tool call must be canonical JSON") from exc


def validate_pending_actions(
    db: Session,
    *,
    run: AgentRunModel,
    reply_id: str,
    input_value: Any,
    confirmation_scope: ConfirmationScope,
) -> None:
    kind = input_value.get("type")
    expected_kind = "human" if kind == "USER_CONFIRM_RESULT" else "external"
    pending = list(
        db.scalars(
            select(RuntimePendingActionModel).where(
                RuntimePendingActionModel.run_id == run.run_id,
                RuntimePendingActionModel.reply_id == reply_id,
                RuntimePendingActionModel.kind == expected_kind,
                RuntimePendingActionModel.status == "pending",
            )
        ).all()
    )
    field = "confirm_results" if kind == "USER_CONFIRM_RESULT" else "execution_results"
    submitted = input_value.get(field)
    if not isinstance(submitted, list) or not submitted:
        raise HITLValidationError("Decision must resolve at least one pending tool call")
    expected = {item.tool_call_id: item for item in pending}
    seen: set[str] = set()
    grants: list[tuple[JsonObject, RuntimePendingActionModel, list[JsonObject]]] = []
    for result in submitted:
        if not isinstance(result, dict):
            raise HITLValidationError("Decision item must be an object")
        tool_call = result.get("tool_call") if kind == "USER_CONFIRM_RESULT" else result
        if not isinstance(tool_call, dict):
            raise HITLValidationError("Decision item is missing the original tool call identity")
        tool_call_id = tool_call.get("id")
        if not isinstance(tool_call_id, str) or tool_call_id not in expected or tool_call_id in seen:
            raise HITLValidationError("Decision references an unknown or duplicate tool call")
        expected_call = expected[tool_call_id]
        if kind == "USER_CONFIRM_RESULT":
            _validate_confirmation(result, tool_call, expected_call)
            if confirmation_scope is ConfirmationScope.RUN:
                if result["confirmed"] is not True:
                    raise HITLValidationError("Run-scoped approval cannot be combined with a denied tool call")
                try:
                    rules = governed_run_permission_rules(tool_call, run.run_id)
                except ValueError as exc:
                    raise HITLValidationError(str(exc)) from exc
                grants.append((result, expected_call, rules))
        else:
            _validate_external_result(tool_call, expected_call)
        seen.add(tool_call_id)
    now = utc_now()
    for result, item, rules in grants:
        result["rules"] = rules
        item.run_rules_json = rules
        item.run_rules_granted_at = now
        item.run_rules_expired_at = None
    for tool_call_id in seen:
        item = expected[tool_call_id]
        item.status = "resolved"
        item.updated_at = now


def expire_run_permission_rules(db: Session, run_id: str, expired_at: str) -> None:
    actions = db.scalars(
        select(RuntimePendingActionModel).where(
            RuntimePendingActionModel.run_id == run_id,
            RuntimePendingActionModel.run_rules_granted_at.is_not(None),
            RuntimePendingActionModel.run_rules_expired_at.is_(None),
        )
    ).all()
    for action in actions:
        action.run_rules_expired_at = expired_at


def _validate_confirmation(
    result: JsonObject,
    tool_call: JsonObject,
    expected: RuntimePendingActionModel,
) -> None:
    if not isinstance(result.get("confirmed"), bool):
        raise HITLValidationError("Confirmation result must include a boolean confirmed value")
    if result.get("rules") not in (None, []):
        raise HITLValidationError("Persistent permission rules cannot be submitted by clients")
    submitted = tool_call_fingerprint(
        tool_call,
        default_state=_default_tool_call_state(expected.kind),
    )
    persisted = parse_tool_call_fingerprint(
        expected.tool_call_json,
        expected_id=expected.tool_call_id,
        expected_name=expected.tool_call_name,
    )
    if submitted != persisted:
        raise HITLValidationError("Canonical tool call cannot be modified during approval")


def _validate_external_result(tool_call: JsonObject, expected: RuntimePendingActionModel) -> None:
    if tool_call.get("type", "tool_result") != "tool_result":
        raise HITLValidationError("External result must be an AgentScope tool_result")
    if tool_call.get("name") != expected.tool_call_name:
        raise HITLValidationError("External result tool name does not match the pending call")
    if tool_call.get("state") not in {"success", "error", "interrupted", "denied"}:
        raise HITLValidationError("External result must carry a terminal tool state")
    if "output" not in tool_call:
        raise HITLValidationError("External result must include output")
