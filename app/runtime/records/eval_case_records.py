from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import EvalCaseGovernanceEventModel, EvalCaseModel, EvalCaseRevisionModel
from app.runtime.state_machines import EVAL_CASE_PROMOTION_STATES, EVAL_CASE_STATES, validate_transition

from .json_types import JsonObject, StrictRuntimeRecord


ACTIVE_ASSET_LAYERS = {"batch_specific", "smoke", "core_regression", "scenario_pack", "safety", "historical_bug"}
ASSET_LAYERS = {"candidate", *ACTIVE_ASSET_LAYERS, "exploratory"}
PROMOTION_STATUSES = EVAL_CASE_PROMOTION_STATES
BLOCKING_POLICIES = {"blocking", "blocking_if_relevant", "non_blocking"}
FLAKY_STATUSES = {"stable", "flaky"}

EvalCaseStatus = Literal["draft", "active", "archived"]
EvalCasePromotionStatus = Literal["candidate", "needs_review", "approved", "rejected", "superseded", "archived"]
EvalCaseAssetLayer = Literal[
    "candidate",
    "batch_specific",
    "smoke",
    "core_regression",
    "scenario_pack",
    "safety",
    "historical_bug",
    "exploratory",
]
EvalCaseBlockingPolicy = Literal["blocking", "blocking_if_relevant", "non_blocking"]
EvalCaseFlakyStatus = Literal["stable", "flaky"]


class EvalCaseRecord(StrictRuntimeRecord):
    """Internal source of truth for one eval case row."""

    schema_version: str = "feedback-eval-case/v1"
    eval_case_id: str
    created_at: str
    updated_at: str
    status: EvalCaseStatus
    source: Optional[str] = None
    source_feedback_case_id: Optional[str] = None
    source_run_id: Optional[str] = None
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    source_refs: list[JsonObject] = Field(default_factory=list)
    asset_layer: EvalCaseAssetLayer
    promotion_status: EvalCasePromotionStatus
    blocking_policy: EvalCaseBlockingPolicy
    scenario_pack: Optional[str] = None
    severity: str = "medium"
    flaky_status: EvalCaseFlakyStatus = "stable"
    variant_role: str = "original_reproduction"
    content_hash: Optional[str] = None
    last_run_at: Optional[str] = None
    last_result_status: Optional[str] = None
    failure_rate: Optional[float] = None
    superseded_by_eval_case_id: Optional[str] = None
    prompt: str
    labels: list[str] = Field(default_factory=list)
    expected_behavior: Optional[str] = None
    checks_json: JsonObject = Field(default_factory=dict)
    source_summary: Optional[JsonObject] = None
    attribution_summary: Optional[JsonObject] = None
    proposal_summary: Optional[JsonObject] = None
    source_signal_ids: list[str] = Field(default_factory=list)
    source_evidence_package_id: Optional[str] = None
    source_attribution_job_id: Optional[str] = None
    source_proposal_job_id: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in EVAL_CASE_STATES:
            raise ValueError(f"unsupported eval case status: {value}")
        return value

    @field_validator("promotion_status")
    @classmethod
    def validate_promotion_status(cls, value: str) -> str:
        if value not in EVAL_CASE_PROMOTION_STATES:
            raise ValueError(f"unsupported eval case promotion_status: {value}")
        return value

    @field_validator("labels", "source_signal_ids")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def validate_shape(self) -> "EvalCaseRecord":
        for key, value in (
            ("eval_case_id", self.eval_case_id),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("prompt", self.prompt),
            ("severity", self.severity),
            ("variant_role", self.variant_role),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        if self.failure_rate is not None and not 0 <= self.failure_rate <= 1:
            raise ValueError("eval case failure_rate must be between 0 and 1")
        if self.promotion_status == "superseded" and not self.superseded_by_eval_case_id:
            raise ValueError("superseded eval cases require superseded_by_eval_case_id")
        return self

    def transition_to(self, *, status: str, promotion_status: str) -> "EvalCaseRecord":
        validate_transition("eval_case", self.status, status)
        validate_transition("eval_case_promotion", self.promotion_status, promotion_status)
        payload = self.to_payload()
        payload["status"] = status
        payload["promotion_status"] = promotion_status
        return type(self).model_validate(payload)

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: EvalCaseModel) -> "EvalCaseRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "eval_case_id": row.eval_case_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "source_feedback_case_id": row.source_feedback_case_id,
                "source_run_id": row.source_run_id,
                "asset_layer": row.asset_layer or payload.get("asset_layer"),
                "promotion_status": row.promotion_status or payload.get("promotion_status"),
                "blocking_policy": row.blocking_policy or payload.get("blocking_policy"),
                "scenario_pack": row.scenario_pack,
                "severity": row.severity or payload.get("severity"),
                "flaky_status": row.flaky_status or payload.get("flaky_status"),
                "variant_role": row.variant_role or payload.get("variant_role"),
                "content_hash": row.content_hash or payload.get("content_hash"),
                "last_run_at": row.last_run_at,
                "last_result_status": row.last_result_status,
                "failure_rate": row.failure_rate,
                "superseded_by_eval_case_id": row.superseded_by_eval_case_id,
                "labels": list(row.labels_json or payload.get("labels") or []),
            }
        )
        return cls.model_validate(payload)


def apply_eval_case_record(row: EvalCaseModel, record: EvalCaseRecord) -> None:
    row.updated_at = record.updated_at
    row.status = record.status
    row.source_feedback_case_id = record.source_feedback_case_id
    row.source_run_id = record.source_run_id
    row.asset_layer = record.asset_layer
    row.promotion_status = record.promotion_status
    row.blocking_policy = record.blocking_policy
    row.scenario_pack = record.scenario_pack
    row.severity = record.severity
    row.flaky_status = record.flaky_status
    row.variant_role = record.variant_role
    row.content_hash = record.content_hash
    row.last_run_at = record.last_run_at
    row.last_result_status = record.last_result_status
    row.failure_rate = record.failure_rate
    row.superseded_by_eval_case_id = record.superseded_by_eval_case_id
    row.labels_json = list(record.labels)
    row.payload_json = record.to_payload()


class EvalCaseRevisionRecord(StrictRuntimeRecord):
    """Internal source of truth for one eval case revision row."""

    revision_id: str
    eval_case_id: str
    revision_number: int
    created_at: str
    created_by: str
    reason: Optional[str] = None
    content_hash: Optional[str] = None
    snapshot: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> "EvalCaseRevisionRecord":
        for key, value in (
            ("revision_id", self.revision_id),
            ("eval_case_id", self.eval_case_id),
            ("created_at", self.created_at),
            ("created_by", self.created_by),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        if self.revision_number < 1:
            raise ValueError("revision_number must be positive")
        return self

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: EvalCaseRevisionModel) -> "EvalCaseRevisionRecord":
        return cls.model_validate(
            {
                "revision_id": row.revision_id,
                "eval_case_id": row.eval_case_id,
                "revision_number": row.revision_number,
                "created_at": row.created_at,
                "created_by": row.created_by,
                "reason": row.reason,
                "content_hash": row.content_hash,
                "snapshot": row.snapshot_json or {},
            }
        )


class EvalCaseGovernanceEventRecord(StrictRuntimeRecord):
    """Internal source of truth for one eval case governance event row."""

    event_id: str
    eval_case_id: str
    action: str
    operator: str
    role: str
    reason: str
    created_at: str
    before: JsonObject = Field(default_factory=dict)
    after: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> "EvalCaseGovernanceEventRecord":
        for key, value in (
            ("event_id", self.event_id),
            ("eval_case_id", self.eval_case_id),
            ("action", self.action),
            ("operator", self.operator),
            ("role", self.role),
            ("reason", self.reason),
            ("created_at", self.created_at),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        return self

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: EvalCaseGovernanceEventModel) -> "EvalCaseGovernanceEventRecord":
        return cls.model_validate(
            {
                "event_id": row.event_id,
                "eval_case_id": row.eval_case_id,
                "action": row.action,
                "operator": row.operator,
                "role": row.role,
                "reason": row.reason,
                "created_at": row.created_at,
                "before": row.before_json or {},
                "after": row.after_json or {},
            }
        )
