from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Optional, TypeAlias

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import (
    AgentRunModel,
    FeedbackSignalModel,
    FeedbackSourceAnnotationModel,
    PendingCorrelationModel,
    SocEventModel,
)
from app.runtime.state_machines import PENDING_CORRELATION_STATES, validate_transition

from ..json_types import JsonObject
from .base import StrictRuntimeRecord

PendingCorrelationStatus = Literal["pending", "resolved"]
FeedbackSourceKind = Literal["signal", "soc_event", "pending_correlation"]
FeedbackSourceAnnotationStatus = Literal["new", "triaged", "in_batch", "resolved", "archived"]
FeedbackPriority = Literal["high", "medium", "low"]
FeedbackSignalSourceType = Literal["explicit_feedback", "implicit_feedback", "analyst_annotation"]
FeedbackConfidence = Literal["low", "medium", "high"]
SocEventType = Literal[
    "case.verdict_changed",
    "case.severity_changed",
    "recommendation.accepted",
    "recommendation.rejected",
    "recommendation.modified",
    "evidence.added",
    "tool.manual_query_after_agent",
]
SocEventEntities: TypeAlias = dict[str, list[str]]


class AgentRunRecord(StrictRuntimeRecord):
    """Internal source of truth for one captured agent run row."""

    run_id: str
    created_at: str
    session_id: Optional[str] = None
    sdk_session_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    completed_at: Optional[str] = None
    langfuse_trace_id: Optional[str] = None
    langfuse_trace_url: Optional[str] = None
    payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> AgentRunRecord:
        if not self.run_id.strip():
            raise ValueError("run_id cannot be empty")
        if not self.created_at.strip():
            raise ValueError("created_at cannot be empty")
        return self

    def to_payload(self) -> JsonObject:
        payload = dict(self.payload)
        payload.update(
            {
                "run_id": self.run_id,
                "created_at": self.created_at,
                "session_id": self.session_id,
                "sdk_session_id": self.sdk_session_id,
                "agent_version_id": self.agent_version_id,
                "alert_id": self.alert_id,
                "case_id": self.case_id,
                "completed_at": self.completed_at,
                "langfuse_trace_id": self.langfuse_trace_id,
                "langfuse_trace_url": self.langfuse_trace_url,
            }
        )
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AgentRunRecord:
        raw_payload = dict(payload)
        return cls.model_validate(
            {
                "run_id": raw_payload.get("run_id"),
                "created_at": raw_payload.get("created_at"),
                "session_id": raw_payload.get("session_id"),
                "sdk_session_id": raw_payload.get("sdk_session_id"),
                "agent_version_id": raw_payload.get("agent_version_id"),
                "alert_id": raw_payload.get("alert_id"),
                "case_id": raw_payload.get("case_id"),
                "completed_at": raw_payload.get("completed_at"),
                "langfuse_trace_id": raw_payload.get("langfuse_trace_id"),
                "langfuse_trace_url": raw_payload.get("langfuse_trace_url"),
                "payload": raw_payload,
            }
        )

    @classmethod
    def from_row(cls, row: AgentRunModel) -> AgentRunRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "run_id": row.run_id,
                "session_id": row.session_id,
                "sdk_session_id": row.sdk_session_id,
                "agent_version_id": row.agent_version_id,
                "alert_id": row.alert_id,
                "case_id": row.case_id,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "langfuse_trace_id": row.langfuse_trace_id,
                "langfuse_trace_url": row.langfuse_trace_url,
            }
        )
        return cls.from_payload(payload)


def upsert_agent_run_record(db: Any, record: AgentRunRecord) -> None:
    """Project one validated run record inside the caller's transaction."""
    values = {
        "session_id": record.session_id,
        "sdk_session_id": record.sdk_session_id,
        "agent_version_id": record.agent_version_id,
        "alert_id": record.alert_id,
        "case_id": record.case_id,
        "created_at": record.created_at,
        "completed_at": record.completed_at,
        "langfuse_trace_id": record.langfuse_trace_id,
        "langfuse_trace_url": record.langfuse_trace_url,
        "payload_json": record.to_payload(),
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
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
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
        if not any((self.run_id, self.session_id, self.alert_id, self.case_id)):
            raise ValueError("feedback signal requires run_id, session_id, alert_id, or case_id")
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
                "alert_id": row.alert_id,
                "case_id": row.case_id,
                "created_at": row.created_at,
            }
        )
        return cls.model_validate(payload)


class SocEventRecord(StrictRuntimeRecord):
    """Internal source of truth for one SOC event row."""

    event_id: str
    source_system: str
    event_type: SocEventType
    timestamp: str
    created_at: str
    matched_run_id: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
    actor_id: Optional[str] = None
    before: Optional[JsonObject] = None
    after: Optional[JsonObject] = None
    entities: SocEventEntities = Field(default_factory=dict)
    auto_captured: bool = True
    confidence: Optional[FeedbackConfidence] = "medium"
    requires_review: bool = True
    comment: Optional[str] = None
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("entities")
    @classmethod
    def validate_entities(cls, value: SocEventEntities) -> SocEventEntities:
        return {str(key): [str(item) for item in items if item] for key, items in value.items() if isinstance(items, list)}

    @model_validator(mode="after")
    def validate_shape(self) -> SocEventRecord:
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

    @classmethod
    def from_row(cls, row: SocEventModel) -> SocEventRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "source_system": row.source_system,
                "run_id": row.run_id,
                "matched_run_id": row.matched_run_id,
                "session_id": row.session_id,
                "alert_id": row.alert_id,
                "case_id": row.case_id,
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
    alert_id: Optional[str] = None
    case_id: Optional[str] = None
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
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
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
                "alert_id": alert_id or self.alert_id,
                "case_id": case_id or self.case_id,
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
