"""AgentScope HITL continuation validation and run-scoped rule projection."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import utc_now

from .contracts import ConfirmationScope, governed_run_permission_rules
from .models import AgentRunModel, RuntimePendingActionModel


class HITLValidationError(ValueError):
    """The continuation differs from the exact persisted pending action."""


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
                    rules = governed_run_permission_rules(expected_call.tool_call_json, run.run_id)
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
    same_input = _canonical_tool_input(tool_call.get("input")) == _canonical_tool_input(expected.tool_call_json.get("input"))
    if tool_call.get("name") != expected.tool_call_name or not same_input:
        raise HITLValidationError("Tool name or normalized input cannot be modified during approval")


def _validate_external_result(tool_call: JsonObject, expected: RuntimePendingActionModel) -> None:
    if tool_call.get("type", "tool_result") != "tool_result":
        raise HITLValidationError("External result must be an AgentScope tool_result")
    if tool_call.get("name") != expected.tool_call_name:
        raise HITLValidationError("External result tool name does not match the pending call")
    if tool_call.get("state") not in {"success", "error", "interrupted", "denied"}:
        raise HITLValidationError("External result must carry a terminal tool state")
    if "output" not in tool_call:
        raise HITLValidationError("External result must include output")


def _canonical_tool_input(value: object) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
