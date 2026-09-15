from __future__ import annotations

import pytest
from agentscope.event import ExternalExecutionResultEvent, UserConfirmResultEvent
from agentscope.message import Msg
from app.runtime_gateway.native_chat_input import (
    RuntimeChatRequest,
    explicit_native_input_ids,
    native_operation_key,
    native_operation_kind,
)
from app.runtime_gateway.operation_identity import RuntimeChatOperationKind
from pydantic import ValidationError


def _message(message_id: str | None = None) -> dict:
    message = {"name": "user", "role": "user", "content": [{"type": "text", "text": "你好"}]}
    if message_id is not None:
        message["id"] = message_id
    return message


def test_native_request_has_only_three_required_fields_and_preserves_unset_ids():
    body = {"agent_id": "runtime-a", "session_id": "session-a", "input": _message()}
    request = RuntimeChatRequest.model_validate(body)
    assert isinstance(request.input, Msg)
    assert request.input.id
    assert request.raw_input == body["input"]
    assert explicit_native_input_ids(request.raw_input) is None
    schema = RuntimeChatRequest.model_json_schema()
    assert set(schema["properties"]) == set(schema["required"]) == {"agent_id", "session_id", "input"}
    assert RuntimeChatRequest.model_validate({**body, "input": None}).raw_input is None
    with pytest.raises(ValidationError):
        RuntimeChatRequest.model_validate({"agent_id": "runtime-a", "session_id": "session-a"})


@pytest.mark.parametrize("field", ["client_operation_id", "expected_run_id", "confirmation_scope", "alert_id", "case_id", "metadata"])
def test_native_request_rejects_removed_governance_fields(field):
    with pytest.raises(ValidationError):
        RuntimeChatRequest.model_validate({"agent_id": "runtime-a", "session_id": "session-a", "input": _message("input-1"), field: "removed"})


@pytest.mark.parametrize(
    "event_type,results,model",
    [
        ("USER_CONFIRM_RESULT", "confirm_results", UserConfirmResultEvent),
        ("EXTERNAL_EXECUTION_RESULT", "execution_results", ExternalExecutionResultEvent),
    ],
)
def test_native_request_uses_public_event_models_without_generated_retry_ids(event_type, results, model):
    value = {"type": event_type, "reply_id": "reply-1", results: []}
    parsed = RuntimeChatRequest.model_validate({"agent_id": "runtime-a", "session_id": "session-a", "input": value})
    assert isinstance(parsed.input, model)
    assert parsed.raw_input == value
    assert explicit_native_input_ids(parsed.raw_input) is None
    assert native_operation_kind(parsed.raw_input) is not RuntimeChatOperationKind.INITIAL
    without_type = {key: value for key, value in value.items() if key != "type"}
    defaulted = RuntimeChatRequest.model_validate({"agent_id": "runtime-a", "session_id": "session-a", "input": without_type})
    assert defaulted.raw_input == value
    assert explicit_native_input_ids(defaulted.raw_input) is None
    assert native_operation_kind(defaulted.raw_input) is not RuntimeChatOperationKind.INITIAL


def test_native_identity_is_scoped_ordered_and_does_not_deduplicate_text():
    inputs = [_message("input-1"), _message("input-2")]
    parsed = RuntimeChatRequest.model_validate({"agent_id": "runtime-a", "session_id": "session-a", "input": inputs})
    assert all(isinstance(message, Msg) for message in parsed.input)
    ids = explicit_native_input_ids(parsed.raw_input)
    assert ids == ("input-1", "input-2")
    kwargs = {"runtime_agent_id": "runtime-a", "session_id": "session-a", "operation_kind": RuntimeChatOperationKind.INITIAL, "input_ids": ids}
    key = native_operation_key(**kwargs)
    assert key == native_operation_key(**kwargs)
    for change in (
        {"session_id": "session-b"},
        {"runtime_agent_id": "runtime-b"},
        {"input_ids": tuple(reversed(ids))},
        {"operation_kind": RuntimeChatOperationKind.USER_CONFIRMATION},
    ):
        assert native_operation_key(**{**kwargs, **change}) != key
    assert explicit_native_input_ids([_message("input-1"), _message()]) is None
    assert explicit_native_input_ids([]) is None
    assert explicit_native_input_ids(None) is None
