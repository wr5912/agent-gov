"""Durable identity and response ledger for Runtime chat operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from ._store_support import (
    RuntimeInputRejected,
    RuntimeObjectNotFound,
    RuntimeStateConflict,
    _detached,
    _require_run,
    _run_response,
    _transition,
)
from .client import RuntimeHeaders
from .contracts import AgentRunResponse, ConfirmationScope, RunStatus
from .models import (
    AgentRunModel,
    RuntimeChatOperationModel,
    RuntimePendingActionModel,
)
from .operation_identity import (
    RuntimeChatOperationKind,
    canonical_request_fingerprint,
    chat_request_fingerprint,
    continuation_operation_key,
    initial_operation_key,
)


@dataclass(frozen=True)
class RuntimeChatReplayResponse:
    status_code: int
    body: bytes
    content_type: str
    headers: RuntimeHeaders


@dataclass(frozen=True)
class RuntimeContinuationIdentity:
    operation_key: str
    operation_kind: RuntimeChatOperationKind
    request_fingerprint: str
    reply_id: str
    action_session_id: str
    action_ids: list[str]
    tool_call_ids: list[str]


@dataclass(frozen=True)
class _StoredChatResponse:
    status_code: int
    body: bytes
    content_type: str
    headers: RuntimeHeaders


@dataclass(frozen=True)
class _ContinuationRequestShape:
    operation_kind: RuntimeChatOperationKind
    pending_kind: str
    reply_id: str
    submitted: list[object]


@dataclass(frozen=True)
class _ContinuationActions:
    action_session_id: str
    action_ids: list[str]
    tool_call_ids: list[str]


class RuntimeChatOperationStoreMixin:
    Session: sessionmaker

    def chat_operation_for_key(
        self,
        operation_key: str,
    ) -> RuntimeChatOperationModel:
        with self.Session() as db:
            operation = db.get(RuntimeChatOperationModel, operation_key)
            if operation is None:
                raise RuntimeObjectNotFound("Runtime chat operation not found")
            return _detached(db, operation)

    def record_chat_operation_response(
        self,
        operation_key: str,
        *,
        run_id: str,
        response_status: int,
        response_body: bytes,
        response_content_type: str,
        response_headers: RuntimeHeaders,
    ) -> AgentRunResponse:
        """Atomically seal one operation response without touching another operation."""

        requested = _StoredChatResponse(
            status_code=response_status,
            body=response_body,
            content_type=response_content_type,
            headers=response_headers,
        )
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            operation = _require_operation(db, operation_key)
            run = _require_run(db, run_id)
            if operation.run_id != run.run_id:
                raise RuntimeStateConflict("Runtime chat operation belongs to another run")
            existing = _operation_response_parts(operation)
            if existing is not None and existing != requested:
                raise RuntimeStateConflict("Runtime chat operation response is immutable")
            if existing is None:
                operation.response_status = requested.status_code
                operation.response_body = requested.body
                operation.response_content_type = requested.content_type
                operation.response_headers_json = dict(requested.headers)
                operation.updated_at = utc_now()
            if operation.operation_kind == RuntimeChatOperationKind.INITIAL.value:
                if RunStatus(run.status) == RunStatus.QUEUED:
                    _transition(run, RunStatus.RUNNING)
                run.started_at = run.started_at or utc_now()
                run.updated_at = utc_now()
            return _run_response(run)


def governed_run_metadata(metadata: JsonObject) -> JsonObject:
    backend_keys = {
        "cancellation_requested",
        "cancellation_uncertain",
        "recovery_required",
        "recovery_quiescent_observations",
        "recovery_interrupt_requested",
        "runtime_boot_id",
        "runtime_boot_version",
        "runtime_interrupted_session_ids",
        "trigger_uncertain",
        "client_operation_id",
    }
    return {key: value for key, value in metadata.items() if key not in backend_keys}


def initial_request_fingerprint(
    *,
    input_value: Any,
    metadata: JsonObject,
    session_id: str,
    runtime_agent_id: str,
    alert_id: str | None,
    case_id: str | None,
) -> str:
    del session_id, runtime_agent_id, alert_id, case_id
    try:
        return canonical_request_fingerprint(
            {"input": input_value, "metadata": metadata},
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeInputRejected(
            "Runtime chat input and metadata must be canonical JSON",
        ) from exc


def find_initial_operation(
    db: Session,
    client_operation_id: str | None,
) -> RuntimeChatOperationModel | None:
    if client_operation_id is None:
        return None
    return db.get(RuntimeChatOperationModel, initial_operation_key(client_operation_id))


def add_initial_operation(
    db: Session,
    *,
    run: AgentRunModel,
    client_operation_id: str | None,
    request_fingerprint: str,
) -> str | None:
    if client_operation_id is None:
        return None
    operation_key = initial_operation_key(client_operation_id)
    db.add(
        RuntimeChatOperationModel(
            operation_key=operation_key,
            client_operation_id=client_operation_id,
            operation_kind=RuntimeChatOperationKind.INITIAL.value,
            request_fingerprint=request_fingerprint,
            run_id=run.run_id,
            root_session_id=run.session_id,
            action_session_id=run.session_id,
            runtime_agent_id=run.runtime_agent_id,
            reply_id=None,
            action_ids_json=[],
            tool_call_ids_json=[],
            confirmation_scope=None,
        ),
    )
    return operation_key


def validate_initial_operation(
    operation: RuntimeChatOperationModel,
    *,
    client_operation_id: str,
    request_fingerprint: str,
    session_id: str,
    runtime_agent_id: str,
) -> None:
    expected = (
        RuntimeChatOperationKind.INITIAL.value,
        client_operation_id,
        request_fingerprint,
        session_id,
        session_id,
        runtime_agent_id,
        None,
        [],
        [],
        None,
    )
    actual = (
        operation.operation_kind,
        operation.client_operation_id,
        operation.request_fingerprint,
        operation.root_session_id,
        operation.action_session_id,
        operation.runtime_agent_id,
        operation.reply_id,
        list(operation.action_ids_json or []),
        list(operation.tool_call_ids_json or []),
        operation.confirmation_scope,
    )
    if actual != expected:
        raise RuntimeStateConflict(
            "client_operation_id is bound to another immutable chat request",
        )


def resolve_continuation_identity(
    db: Session,
    *,
    run: AgentRunModel,
    input_value: Any,
    metadata: JsonObject,
    session_id: str,
    runtime_agent_id: str,
    alert_id: str | None,
    case_id: str | None,
    client_operation_id: str,
    confirmation_scope: ConfirmationScope,
) -> RuntimeContinuationIdentity:
    shape = _continuation_request_shape(input_value)
    actions = _continuation_actions(db, run=run, shape=shape)
    try:
        request_fingerprint = chat_request_fingerprint(
            input_value=input_value,
            metadata=metadata,
            session_id=session_id,
            runtime_agent_id=runtime_agent_id,
            alert_id=alert_id,
            case_id=case_id,
            expected_run_id=run.run_id,
            confirmation_scope=confirmation_scope.value,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeInputRejected(
            "Runtime chat input and metadata must be canonical JSON",
        ) from exc
    return RuntimeContinuationIdentity(
        operation_key=continuation_operation_key(
            client_operation_id=client_operation_id,
            operation_kind=shape.operation_kind,
            action_session_id=actions.action_session_id,
            reply_id=shape.reply_id,
            action_ids=actions.action_ids,
            tool_call_ids=actions.tool_call_ids,
        ),
        operation_kind=shape.operation_kind,
        request_fingerprint=request_fingerprint,
        reply_id=shape.reply_id,
        action_session_id=actions.action_session_id,
        action_ids=actions.action_ids,
        tool_call_ids=actions.tool_call_ids,
    )


def add_continuation_operation(
    db: Session,
    *,
    run: AgentRunModel,
    client_operation_id: str,
    confirmation_scope: ConfirmationScope,
    identity: RuntimeContinuationIdentity,
) -> None:
    db.add(
        RuntimeChatOperationModel(
            operation_key=identity.operation_key,
            client_operation_id=client_operation_id,
            operation_kind=identity.operation_kind.value,
            request_fingerprint=identity.request_fingerprint,
            run_id=run.run_id,
            root_session_id=run.session_id,
            action_session_id=identity.action_session_id,
            runtime_agent_id=run.runtime_agent_id,
            reply_id=identity.reply_id,
            action_ids_json=identity.action_ids,
            tool_call_ids_json=identity.tool_call_ids,
            confirmation_scope=confirmation_scope.value,
        ),
    )


def validate_continuation_operation(
    operation: RuntimeChatOperationModel,
    *,
    run: AgentRunModel,
    client_operation_id: str,
    confirmation_scope: ConfirmationScope,
    identity: RuntimeContinuationIdentity,
) -> None:
    expected = (
        client_operation_id,
        identity.operation_kind.value,
        identity.request_fingerprint,
        run.run_id,
        run.session_id,
        identity.action_session_id,
        run.runtime_agent_id,
        identity.reply_id,
        identity.action_ids,
        identity.tool_call_ids,
        confirmation_scope.value,
    )
    actual = (
        operation.client_operation_id,
        operation.operation_kind,
        operation.request_fingerprint,
        operation.run_id,
        operation.root_session_id,
        operation.action_session_id,
        operation.runtime_agent_id,
        operation.reply_id,
        list(operation.action_ids_json or []),
        list(operation.tool_call_ids_json or []),
        operation.confirmation_scope,
    )
    if actual != expected:
        raise RuntimeStateConflict(
            "client_operation_id is bound to another immutable continuation request",
        )


def operation_replay_response(
    operation: RuntimeChatOperationModel,
) -> RuntimeChatReplayResponse | None:
    parts = _operation_response_parts(operation)
    if parts is None:
        return None
    return RuntimeChatReplayResponse(
        status_code=parts.status_code,
        body=parts.body,
        content_type=parts.content_type,
        headers=parts.headers,
    )


def _require_operation(
    db: Session,
    operation_key: str,
) -> RuntimeChatOperationModel:
    operation = db.get(RuntimeChatOperationModel, operation_key)
    if operation is None:
        raise RuntimeObjectNotFound("Runtime chat operation not found")
    return operation


def _operation_response_parts(
    operation: RuntimeChatOperationModel,
) -> _StoredChatResponse | None:
    raw_parts = (
        operation.response_status,
        operation.response_body,
        operation.response_content_type,
        operation.response_headers_json,
    )
    if all(value is None for value in raw_parts):
        return None
    if any(value is None for value in raw_parts):
        raise RuntimeStateConflict("Runtime chat operation has a partial response ledger")
    status = operation.response_status
    body = operation.response_body
    content_type = operation.response_content_type
    raw_headers = operation.response_headers_json
    if not isinstance(status, int) or not isinstance(body, bytes) or not isinstance(content_type, str) or not isinstance(raw_headers, dict):
        raise RuntimeStateConflict("Runtime chat operation response ledger is invalid")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_headers.items()):
        raise RuntimeStateConflict("Runtime chat operation response headers are invalid")
    headers: RuntimeHeaders = {str(key): value for key, value in raw_headers.items()}
    return _StoredChatResponse(
        status_code=status,
        body=body,
        content_type=content_type,
        headers=headers,
    )


def _continuation_request_shape(input_value: object) -> _ContinuationRequestShape:
    if not isinstance(input_value, dict):  # pragma: no cover - caller checks
        raise RuntimeStateConflict("HITL continuation must be an object")
    native_kind = input_value.get("type")
    if native_kind == "USER_CONFIRM_RESULT":
        operation_kind = RuntimeChatOperationKind.USER_CONFIRMATION
        pending_kind = "human"
        result_field = "confirm_results"
    elif native_kind == "EXTERNAL_EXECUTION_RESULT":
        operation_kind = RuntimeChatOperationKind.EXTERNAL_EXECUTION
        pending_kind = "external"
        result_field = "execution_results"
    else:  # pragma: no cover - caller checks
        raise RuntimeStateConflict("HITL continuation type is invalid")
    reply_id = input_value.get("reply_id")
    submitted = input_value.get(result_field)
    if not isinstance(reply_id, str) or not reply_id:
        raise RuntimeStateConflict("Decision does not identify an active reply")
    if not isinstance(submitted, list) or not submitted:
        raise RuntimeStateConflict("Decision must resolve at least one pending tool call")
    return _ContinuationRequestShape(
        operation_kind=operation_kind,
        pending_kind=pending_kind,
        reply_id=reply_id,
        submitted=submitted,
    )


def _continuation_actions(
    db: Session,
    *,
    run: AgentRunModel,
    shape: _ContinuationRequestShape,
) -> _ContinuationActions:
    tool_call_ids = _submitted_tool_call_ids(
        shape.submitted,
        human=shape.pending_kind == "human",
    )
    rows = list(
        db.scalars(
            select(RuntimePendingActionModel).where(
                RuntimePendingActionModel.run_id == run.run_id,
                RuntimePendingActionModel.reply_id == shape.reply_id,
                RuntimePendingActionModel.tool_call_id.in_(tool_call_ids),
            ),
        ).all(),
    )
    if len(rows) != len(tool_call_ids):
        raise RuntimeStateConflict("Decision references an unknown or ambiguous tool call")
    rows_by_tool = {row.tool_call_id: row for row in rows}
    if len(rows_by_tool) != len(tool_call_ids):
        raise RuntimeStateConflict("Decision references an ambiguous tool call identity")
    if any(row.kind != shape.pending_kind for row in rows):
        raise RuntimeStateConflict("Decision type does not match the pending action kind")
    action_sessions = {row.session_id for row in rows}
    if len(action_sessions) != 1:
        raise RuntimeStateConflict("One continuation cannot cross worker Session boundaries")
    return _ContinuationActions(
        action_session_id=action_sessions.pop(),
        action_ids=sorted(rows_by_tool[tool_id].action_id for tool_id in tool_call_ids),
        tool_call_ids=sorted(tool_call_ids),
    )


def _submitted_tool_call_ids(
    submitted: list[object],
    *,
    human: bool,
) -> list[str]:
    tool_call_ids: list[str] = []
    for result in submitted:
        tool_call = result.get("tool_call") if human and isinstance(result, dict) else result
        tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        if not isinstance(tool_call_id, str) or not tool_call_id or tool_call_id in tool_call_ids:
            raise RuntimeStateConflict("Decision references an unknown or duplicate tool call")
        tool_call_ids.append(tool_call_id)
    return tool_call_ids
