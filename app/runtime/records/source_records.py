from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import (
    AgentRunModel,
    FeedbackEventModel,
    FeedbackSignalModel,
    FeedbackSourceAnnotationModel,
    PendingCorrelationModel,
)
from app.runtime.state_machines import PENDING_CORRELATION_STATES, PendingCorrelationStatus, validate_transition

from ..feedback_entities import FeedbackEntities, merge_entities
from ..json_types import JsonObject
from .base import StrictRuntimeRecord

FeedbackSourceKind = Literal["signal", "event", "pending_correlation"]
FeedbackSourceAnnotationStatus = Literal["new", "triaged", "in_batch", "resolved", "archived"]
FeedbackPriority = Literal["high", "medium", "low"]
FeedbackSignalSourceType = Literal["explicit_feedback", "implicit_feedback", "analyst_annotation"]
FeedbackConfidence = Literal["low", "medium", "high"]
FeedbackEventType = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]


class AgentRunRecord(StrictRuntimeRecord):
    """Internal source of truth for one captured agent run row."""

    run_id: str
    created_at: str
    session_id: str
    agent_id: str
    agent_version_id: str
    runtime_agent_id: str
    harness_digest: str
    status: str
    reply_ids: list[str] = Field(default_factory=list)
    trace_id: Optional[str] = None
    trace_url: Optional[str] = None
    trace_status: str = "pending"
    terminal_reason: Optional[str] = None
    error_json: JsonObject | None = Field(default=None, alias="error", serialization_alias="error")
    entities: FeedbackEntities = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    started_at: Optional[str] = None
    updated_at: str
    completed_at: Optional[str] = None

    @model_validator(mode="after")
    def validate_shape(self) -> AgentRunRecord:
        if not self.run_id.strip():
            raise ValueError("run_id cannot be empty")
        if not self.created_at.strip():
            raise ValueError("created_at cannot be empty")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AgentRunRecord:
        return cls.model_validate(dict(payload))

    @classmethod
    def from_row(cls, row: AgentRunModel) -> AgentRunRecord:
        return cls.model_validate(
            {
                "run_id": row.run_id,
                "session_id": row.session_id,
                "agent_id": row.agent_id,
                "agent_version_id": row.agent_version_id,
                "runtime_agent_id": row.runtime_agent_id,
                "harness_digest": row.harness_digest,
                "status": row.status,
                "reply_ids": list(row.reply_ids_json or []),
                "trace_id": row.trace_id,
                "trace_url": row.trace_url,
                "trace_status": row.trace_status,
                "terminal_reason": row.terminal_reason,
                "error": dict(row.error_json) if row.error_json else None,
                "entities": row.entities_json or {},
                "metadata": dict(row.metadata_json or {}),
                "created_at": row.created_at,
                "started_at": row.started_at,
                "updated_at": row.updated_at,
                "completed_at": row.completed_at,
            }
        )


def upsert_agent_run_record(db: Any, record: AgentRunRecord) -> None:
    """Project one validated run record inside the caller's transaction."""
    values = {
        "session_id": record.session_id,
        "agent_id": record.agent_id,
        "agent_version_id": record.agent_version_id,
        "runtime_agent_id": record.runtime_agent_id,
        "harness_digest": record.harness_digest,
        "status": record.status,
        "reply_ids_json": record.reply_ids,
        "trace_id": record.trace_id,
        "trace_url": record.trace_url,
        "trace_status": record.trace_status,
        "terminal_reason": record.terminal_reason,
        "error_json": record.error_json,
        "entities_json": record.entities,
        "metadata_json": record.metadata,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "updated_at": record.updated_at,
        "completed_at": record.completed_at,
    }
    row = db.get(AgentRunModel, record.run_id)
    if row is None:
        db.add(AgentRunModel(run_id=record.run_id, **values))
        return
    for key, value in values.items():
        setattr(row, key, value)


class FeedbackSignalRecord(StrictRuntimeRecord):
    """Internal source of truth for one feedback signal row."""

    signal_id: str
    created_at: str
    source_type: FeedbackSignalSourceType = "explicit_feedback"
    agent_id: Optional[str] = None
    timestamp: Optional[str] = None
    run_id: Optional[str] = None
    matched_run_id: Optional[str] = None
    session_id: Optional[str] = None
    entities: FeedbackEntities = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    comment: Optional[str] = None
    confidence: Optional[FeedbackConfidence] = None
    auto_captured: bool = False
    requires_review: bool = False
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def validate_shape(self) -> FeedbackSignalRecord:
        if not self.signal_id.strip():
            raise ValueError("signal_id cannot be empty")
        if not self.created_at.strip():
            raise ValueError("created_at cannot be empty")
        corrections = self.metadata.get("attribution_corrections")
        is_manually_reassigned = bool(self.agent_id and isinstance(corrections, list) and corrections)
        if not any((self.run_id, self.session_id, self.entities, is_manually_reassigned)):
            raise ValueError("feedback signal requires run_id, session_id, or entities")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: FeedbackSignalModel) -> FeedbackSignalRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "signal_id": row.signal_id,
                "source_type": row.source_type,
                "agent_id": row.agent_id,
                "run_id": row.run_id,
                "matched_run_id": row.matched_run_id,
                "session_id": row.session_id,
                "created_at": row.created_at,
            }
        )
        return cls.model_validate(payload)


class FeedbackEventRecord(StrictRuntimeRecord):
    """通用业务事件的持久化记录。"""

    event_id: str
    source_system: str
    event_type: FeedbackEventType
    timestamp: str
    created_at: str
    agent_id: Optional[str] = None
    matched_run_id: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    actor_id: Optional[str] = None
    before: Optional[JsonObject] = None
    after: Optional[JsonObject] = None
    entities: FeedbackEntities = Field(default_factory=dict)
    auto_captured: bool = True
    confidence: Optional[FeedbackConfidence] = "medium"
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: JsonObject = Field(default_factory=dict)
    ingestion_request_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$", exclude=True)

    @model_validator(mode="after")
    def validate_shape(self) -> FeedbackEventRecord:
        for key, value in (
            ("event_id", self.event_id),
            ("source_system", self.source_system),
            ("timestamp", self.timestamp),
            ("created_at", self.created_at),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    def to_persistence_payload(self) -> JsonObject:
        payload = self.to_payload()
        if self.ingestion_request_sha256 is not None:
            payload["ingestion_request_sha256"] = self.ingestion_request_sha256
        return payload

    @classmethod
    def from_row(cls, row: FeedbackEventModel) -> FeedbackEventRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "source_system": row.source_system,
                "agent_id": row.agent_id,
                "run_id": row.run_id,
                "matched_run_id": row.matched_run_id,
                "session_id": row.session_id,
                "created_at": row.created_at,
            }
        )
        return cls.model_validate(payload)


class PendingCorrelationRecord(StrictRuntimeRecord):
    """Internal source of truth for one pending source correlation row."""

    pending_id: str
    created_at: str
    updated_at: str
    status: PendingCorrelationStatus
    reason: str
    event_id: str
    event_type: str
    source_system: str
    session_id: Optional[str] = None
    entities: FeedbackEntities = Field(default_factory=dict)
    resolved_run_id: Optional[str] = None
    comment: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PENDING_CORRELATION_STATES:
            raise ValueError(f"unsupported pending correlation status: {value}")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> PendingCorrelationRecord:
        for key, value in (
            ("pending_id", self.pending_id),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("reason", self.reason),
            ("event_id", self.event_id),
            ("event_type", self.event_type),
            ("source_system", self.source_system),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        return self

    def resolve(
        self,
        *,
        updated_at: str,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        entities: FeedbackEntities | None = None,
        comment: Optional[str] = None,
    ) -> PendingCorrelationRecord:
        validate_transition("pending_correlation", self.status, "resolved")
        payload = self.to_payload()
        payload.update(
            {
                "updated_at": updated_at,
                "status": "resolved",
                "resolved_run_id": run_id or self.resolved_run_id,
                "session_id": session_id or self.session_id,
                "entities": merge_entities([self.entities, entities or {}]),
                "comment": comment,
            }
        )
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: PendingCorrelationModel) -> PendingCorrelationRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "pending_id": row.pending_id,
                "event_id": row.event_id,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
        return cls.model_validate(payload)


def apply_pending_correlation_record(row: PendingCorrelationModel, record: PendingCorrelationRecord) -> None:
    row.status = record.status
    row.updated_at = record.updated_at
    row.payload_json = record.to_payload()


class FeedbackEventIngestionRecord(StrictRuntimeRecord):
    event: FeedbackEventRecord
    correlation_status: Literal["matched", "pending_correlation", "duplicate"]
    matched_run_id: str | None = None
    pending_correlation: PendingCorrelationRecord | None = None

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")


class FeedbackSourceAnnotationRecord(StrictRuntimeRecord):
    """Internal source of truth for one source annotation row."""

    annotation_id: str
    source_kind: FeedbackSourceKind
    source_id: str
    created_at: str
    updated_at: str
    status: FeedbackSourceAnnotationStatus = "triaged"
    comment: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    priority: Optional[FeedbackPriority] = None
    requires_review: Optional[bool] = None
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def validate_shape(self) -> FeedbackSourceAnnotationRecord:
        for key, value in (
            ("annotation_id", self.annotation_id),
            ("source_id", self.source_id),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        return self

    def update(self, *, fields: dict[str, object], updated_at: str) -> FeedbackSourceAnnotationRecord:
        payload = self.to_payload()
        payload["updated_at"] = updated_at
        for key in ("comment", "labels", "priority", "status", "requires_review", "metadata"):
            if key in fields:
                payload[key] = fields[key]
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: FeedbackSourceAnnotationModel) -> FeedbackSourceAnnotationRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "annotation_id": row.annotation_id,
                "source_kind": row.source_kind,
                "source_id": row.source_id,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
        return cls.model_validate(payload)


def apply_feedback_source_annotation_record(
    row: FeedbackSourceAnnotationModel,
    record: FeedbackSourceAnnotationRecord,
) -> None:
    row.status = record.status
    row.updated_at = record.updated_at
    row.payload_json = record.to_payload()
