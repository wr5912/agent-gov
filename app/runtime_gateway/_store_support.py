from __future__ import annotations

import json
import secrets
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentgov_harness_digest import harness_content_digest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.runtime.runtime_db_base import utc_now

from .contracts import ALLOWED_RUN_TRANSITIONS, AgentRunResponse, RunStatus
from .hitl import expire_run_permission_rules
from .models import (
    AgentRunModel,
    RuntimeAgentDeletionIntentModel,
    RuntimeEphemeralResourceModel,
    RuntimePendingActionModel,
    RuntimeSessionBindingModel,
    RuntimeSessionCreationIntentModel,
)


class RuntimeStoreError(RuntimeError):
    status_code = 409


class RuntimeObjectNotFound(RuntimeStoreError):
    status_code = 404


class RuntimeStateConflict(RuntimeStoreError):
    status_code = 409


class RuntimeRestartRequired(RuntimeStoreError):
    status_code = 503


class RuntimeInputRejected(RuntimeStoreError):
    status_code = 422


class RuntimeAuthenticationError(RuntimeStoreError):
    status_code = 401


class SessionCreationStatus(StrEnum):
    PENDING = "pending"
    UPSTREAM_CREATED = "upstream_created"
    BOUND = "bound"
    CLEANUP_PENDING = "cleanup_pending"
    FAILED = "failed"
    FAILED_CLEANED = "failed_cleaned"


def harness_digest(workspace: Path) -> str:
    """计算受治理 Harness 的稳定摘要，不包含运行态或 Git 元数据。"""

    return harness_content_digest(workspace)


def _runtime_context_response(run: AgentRunModel, binding: RuntimeSessionBindingModel):
    from .contracts import RuntimeContextResponse

    is_root = binding.session_id == run.session_id
    return RuntimeContextResponse(
        run_id=run.run_id,
        session_id=binding.session_id,
        root_session_id=run.session_id,
        role="root" if is_root else "worker",
        agent_id=run.agent_id,
        agent_version_id=run.agent_version_id,
        runtime_agent_id=binding.runtime_agent_id,
        harness_digest=run.harness_digest,
        trace_id=run.trace_id,
        team_generation=(run.team_generation if is_root else binding.active_team_generation),
    )


def _require_run(db: Session, run_id: str) -> AgentRunModel:
    run = db.get(AgentRunModel, run_id)
    if run is None:
        raise RuntimeObjectNotFound(f"Agent run not found: {run_id}")
    return run


def _transition(run: AgentRunModel, target: RunStatus) -> None:
    current = RunStatus(run.status)
    if target == current:
        return
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise RuntimeStateConflict(f"Illegal run transition: {current.value} -> {target.value}")
    run.status = target.value


def _finish_run(db: Session, run: AgentRunModel) -> None:
    now = utc_now()
    run.completed_at = now
    run.updated_at = now
    bindings = db.scalars(
        select(RuntimeSessionBindingModel).where(
            RuntimeSessionBindingModel.active_run_id == run.run_id,
        ),
    ).all()
    for binding in bindings:
        binding.active_run_id = None
        binding.active_team_generation = 0
        binding.updated_at = now
    pending_actions = db.scalars(
        select(RuntimePendingActionModel).where(
            RuntimePendingActionModel.run_id == run.run_id,
            RuntimePendingActionModel.status == "pending",
        ),
    ).all()
    for action in pending_actions:
        action.status = "expired"
        action.updated_at = now
    run.pending_child_session_ids_json = []
    expire_run_permission_rules(db, run.run_id, now)


def _run_response(row: AgentRunModel) -> AgentRunResponse:
    return AgentRunResponse(
        run_id=row.run_id,
        session_id=row.session_id,
        client_operation_id=row.client_operation_id,
        agent_id=row.agent_id,
        agent_version_id=row.agent_version_id,
        runtime_agent_id=row.runtime_agent_id,
        harness_digest=row.harness_digest,
        status=RunStatus(row.status),
        reply_ids=list(row.reply_ids_json or []),
        persisted_reply_ids=list(row.persisted_reply_ids_json or []),
        persistence_batch_reply_ids=list(row.persistence_batch_reply_ids_json or []),
        team_generation=row.team_generation,
        root_persisted_team_generation=row.root_persisted_team_generation,
        pending_child_session_ids=list(row.pending_child_session_ids_json or []),
        trace_id=row.trace_id,
        trace_url=row.trace_url,
        trace_status=row.trace_status,
        terminal_reason=row.terminal_reason,
        error=dict(row.error_json) if row.error_json else None,
        alert_id=row.alert_id,
        case_id=row.case_id,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
        started_at=row.started_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_json_id(run: AgentRunModel, attribute: str, value: str) -> None:
    values = list(getattr(run, attribute) or [])
    if value not in values:
        values.append(value)
        setattr(run, attribute, values)


def _remove_json_id(run: AgentRunModel, attribute: str, value: str) -> None:
    values = list(getattr(run, attribute) or [])
    if value in values:
        values.remove(value)
        setattr(run, attribute, values)


def _validated_reply_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RuntimeStateConflict("SESSION_PERSISTED requires a non-empty reply_ids batch")
    reply_ids: list[str] = []
    for reply_id in value:
        if not isinstance(reply_id, str) or not reply_id or reply_id in reply_ids:
            raise RuntimeStateConflict("SESSION_PERSISTED reply_ids must be unique non-empty strings")
        reply_ids.append(reply_id)
    return reply_ids


def _validated_team_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeStateConflict("SESSION_PERSISTED requires a non-negative Team generation")
    return value


def _new_trace_id() -> str:
    """生成 OTel 有效的非零 128-bit trace id，供跨 HITL 调用延续。"""

    while True:
        value = secrets.token_hex(16)
        if value != "0" * 32:
            return value


def _error_payload(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        error_type = value.get("type")
        if isinstance(error_type, str) and error_type:
            return {"type": error_type}
    return {"type": "runtime_error"}


def _require_agent_deletion(db: Session, intent_id: str) -> RuntimeAgentDeletionIntentModel:
    intent = db.get(RuntimeAgentDeletionIntentModel, intent_id)
    if intent is None:
        raise RuntimeObjectNotFound(f"Agent deletion intent not found: {intent_id}")
    if intent.status != "cleanup_pending":
        raise RuntimeStateConflict("Agent deletion intent is already complete")
    return intent


def _require_ephemeral_resource(db: Session, cache_key: str) -> RuntimeEphemeralResourceModel:
    resource = db.get(RuntimeEphemeralResourceModel, cache_key)
    if resource is None:
        raise RuntimeObjectNotFound(f"Ephemeral Runtime resource not found: {cache_key}")
    if resource.status == "cleanup_complete":
        raise RuntimeStateConflict("Ephemeral Runtime resource is already complete")
    return resource


def _deletion_snapshot_id(value: dict[str, object]) -> str:
    version_id = value.get("agent_version_id")
    digest = value.get("harness_digest")
    if not isinstance(version_id, str) or not isinstance(digest, str):
        raise RuntimeStateConflict("Agent deletion snapshot tuple is invalid")
    source_kind = value.get("source_kind")
    source_id = value.get("source_id")
    if not isinstance(source_kind, str) or not source_kind:
        raise RuntimeStateConflict("Agent deletion snapshot source kind is invalid")
    if source_id is not None and not isinstance(source_id, str):
        raise RuntimeStateConflict("Agent deletion snapshot source id is invalid")
    return f"{source_kind}:{source_id or ''}:{version_id}:{digest}"


def _require_session_creation(db: Session, intent_id: str) -> RuntimeSessionCreationIntentModel:
    intent = db.get(RuntimeSessionCreationIntentModel, intent_id)
    if intent is None:
        raise RuntimeObjectNotFound(f"Session creation intent not found: {intent_id}")
    return intent


def _version_identity(row: RuntimeSessionBindingModel | RuntimeSessionCreationIntentModel) -> tuple[str, str, str, str]:
    return row.agent_id, row.agent_version_id, row.runtime_agent_id, row.harness_digest


def _base_workspace_id(workspace_id: str) -> str:
    return workspace_id.rsplit("--s-", 1)[0]


def _detached(db: Session, row: Any) -> Any:
    if row is not None:
        db.expunge(row)
    return row
