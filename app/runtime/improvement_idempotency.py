"""改进事项写操作共享的持久化幂等契约。"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .errors import BusinessRuleViolation, ConflictError, DataIntegrityError, IdempotencyKeyConflictError
from .feedback_entities import FeedbackEntities
from .improvement_db import ImprovementIdempotencyOperationModel
from .runtime_db_base import utc_now

CREATE_IMPROVEMENT_OPERATION = "create_improvement"
CREATE_IMPROVEMENT_FEEDBACK_OPERATION = "create_improvement_feedback"


def normalize_idempotency_key(value: str | None) -> str | None:
    """Normalize an optional public retry key without inventing one server-side."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise BusinessRuleViolation("Idempotency-Key cannot be blank")
    if len(normalized) > 256:
        raise BusinessRuleViolation("Idempotency-Key cannot exceed 256 characters")
    return normalized


def improvement_create_request_fingerprint(
    *,
    agent_id: str,
    title: str,
    summary: str,
    source_feedback_refs: list[str],
    auto_merge: bool,
) -> str:
    return _request_fingerprint(
        [agent_id, title, summary, source_feedback_refs, auto_merge],
    )


def feedback_create_request_fingerprint(
    *,
    improvement_id: str,
    summary: str,
    source: str,
    status: str,
    raw_text: str,
    run_id: str,
    session_id: str,
    agent_version_id: str,
    scenario: str,
    task_id: str,
    entities: FeedbackEntities,
) -> str:
    return _request_fingerprint(
        [
            improvement_id,
            summary,
            source,
            status,
            raw_text,
            run_id,
            session_id,
            agent_version_id,
            scenario,
            task_id,
            entities,
        ],
    )


def bind_idempotency_operation(
    db: Session,
    *,
    operation_kind: str,
    key: str,
    request_fingerprint: str,
) -> tuple[ImprovementIdempotencyOperationModel, bool]:
    """原子 claim 一个 key；返回账本行及其是否为已完成重放。"""

    operation_key = hashlib.sha256(f"{operation_kind}\0{key}".encode()).hexdigest()
    now = utc_now()
    result = db.execute(
        insert(ImprovementIdempotencyOperationModel)
        .values(
            operation_key=operation_key,
            operation_kind=operation_kind,
            request_fingerprint=request_fingerprint,
            result_resource_kind="",
            result_resource_id="",
            tombstoned=False,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["operation_key"])
    )
    created = result.rowcount == 1
    row = db.get(ImprovementIdempotencyOperationModel, operation_key)
    if row is None:
        raise DataIntegrityError("Idempotency operation could not be read after claim")
    if row.operation_kind != operation_kind or row.request_fingerprint != request_fingerprint:
        raise IdempotencyKeyConflictError(row.result_resource_kind or operation_kind)
    if not created:
        if row.tombstoned:
            raise ConflictError("Idempotent operation result was deleted and cannot be recreated")
        if not row.result_resource_kind or not row.result_resource_id:
            raise DataIntegrityError("Idempotency operation has no durable result")
    return row, not created


def complete_idempotency_operation(
    row: ImprovementIdempotencyOperationModel,
    *,
    resource_kind: str,
    resource_id: str,
) -> None:
    if row.result_resource_id and (row.result_resource_kind != resource_kind or row.result_resource_id != resource_id):
        raise DataIntegrityError("Idempotency operation result cannot be rebound")
    row.result_resource_kind = resource_kind
    row.result_resource_id = resource_id
    row.updated_at = utc_now()


def tombstone_idempotency_results(
    db: Session,
    *,
    resource_ids: list[str],
) -> None:
    if not resource_ids:
        return
    db.execute(
        update(ImprovementIdempotencyOperationModel)
        .where(
            ImprovementIdempotencyOperationModel.result_resource_id.in_(resource_ids),
            ImprovementIdempotencyOperationModel.tombstoned.is_(False),
        )
        .values(tombstoned=True, updated_at=utc_now())
    )


def _request_fingerprint(parts: list[object]) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
