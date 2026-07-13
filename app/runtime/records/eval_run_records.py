from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.runtime.runtime_db import EvalRunItemModel, EvalRunModel
from app.runtime.state_machines import EVAL_RUN_STATES, validate_transition
from app.runtime.test_dataset_schemas import TestCaseRecord, TestDatasetRecord

from ..json_types import JsonObject
from .base import StrictRuntimeRecord

EvalRunStatus = Literal["running", "completed", "failed"]
EvalRunResultStatus = Literal[
    "running",
    "passed",
    "failed",
    "needs_human_review",
    "blocked",
    "review_required",
    "passed_with_notes",
]

EvalRunItemStatus = Literal["passed", "failed", "needs_human_review"]
EvalRunReviewDecision = Literal["approve", "reject"]


class EvalRunSummaryRecord(StrictRuntimeRecord):
    total: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    needs_human_review: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    review_required: int = Field(default=0, ge=0)
    passed_with_notes: int = Field(default=0, ge=0)


class EvalRunCheckResultRecord(StrictRuntimeRecord):
    name: str
    passed: bool
    required: bool = False
    detail: str = ""


class EvalRunReviewItemDecisionRecord(StrictRuntimeRecord):
    dataset_case_id: str
    decision: EvalRunReviewDecision
    note: str = ""


class EvalRunReviewDecisionRecord(StrictRuntimeRecord):
    review_id: str
    operator: str
    reason: str
    scope: Literal["current_eval_run"] = "current_eval_run"
    items: list[EvalRunReviewItemDecisionRecord]
    created_at: str

    @model_validator(mode="after")
    def validate_unique_case_decisions(self) -> EvalRunReviewDecisionRecord:
        case_ids = [item.dataset_case_id for item in self.items]
        if not self.review_id.strip() or not self.operator.strip() or not self.reason.strip() or not case_ids:
            raise ValueError("EvalRun review requires review_id, operator, reason, and item decisions")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("EvalRun review contains duplicate dataset case decisions")
        return self


class EvalRunGateResultRecord(StrictRuntimeRecord):
    status: str
    blocked_dataset_case_ids: list[str] = Field(default_factory=list)
    review_dataset_case_ids: list[str] = Field(default_factory=list)
    note_dataset_case_ids: list[str] = Field(default_factory=list)
    review_decision: Optional[EvalRunReviewDecisionRecord] = None

    @model_validator(mode="after")
    def validate_review_decision_projection(self) -> EvalRunGateResultRecord:
        review = self.review_decision
        if review is None:
            return self
        accepted = {item.dataset_case_id for item in review.items if item.decision == "approve"}
        rejected = {item.dataset_case_id for item in review.items if item.decision == "reject"}
        if self.review_dataset_case_ids:
            raise ValueError("Reviewed EvalRun gate cannot retain pending review case ids")
        if set(self.note_dataset_case_ids) != accepted or set(self.blocked_dataset_case_ids) != rejected:
            raise ValueError("EvalRun gate review decision does not match projected case ids")
        expected_status = "blocked" if rejected else "passed_with_notes"
        if self.status != expected_status:
            raise ValueError("EvalRun gate review decision does not match gate status")
        return self


class EvalRunRecord(StrictRuntimeRecord):
    """Internal source of truth for persisted eval run payload_json."""

    eval_run_id: str
    dataset_id: str
    dataset_snapshot: TestDatasetRecord
    created_at: str
    completed_at: Optional[str] = None
    status: EvalRunStatus
    result_status: EvalRunResultStatus = "running"
    agent_id: str = "main-agent"
    agent_version_id: Optional[str] = None
    source: str
    change_set_id: Optional[str] = None
    regression_attempt_id: Optional[str] = None
    candidate_commit_sha: Optional[str] = None
    candidate_worktree_path: Optional[str] = None
    runtime_heartbeat_at: Optional[str] = Field(default=None, exclude=True)
    summary: EvalRunSummaryRecord = Field(default_factory=EvalRunSummaryRecord)
    gate_result: EvalRunGateResultRecord = Field(
        default_factory=lambda: EvalRunGateResultRecord(
            status="running",
            blocked_dataset_case_ids=[],
            review_dataset_case_ids=[],
            note_dataset_case_ids=[],
        )
    )
    error_json: Optional[JsonObject] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in EVAL_RUN_STATES:
            raise ValueError(f"unsupported eval run status: {value}")
        return value

    @model_validator(mode="after")
    def validate_lifecycle_shape(self) -> EvalRunRecord:
        if self.dataset_snapshot.dataset_id != self.dataset_id:
            raise ValueError("dataset_snapshot must match dataset_id")
        if self.dataset_snapshot.agent_id != self.agent_id:
            raise ValueError("dataset_snapshot must match eval run agent")
        if not self.dataset_snapshot.cases:
            raise ValueError("dataset_snapshot must contain at least one case")
        if self.status == "running":
            if self.completed_at:
                raise ValueError("completed_at must not be set while eval run is running")
            if self.result_status != "running":
                raise ValueError("running eval runs must have running result_status")
            return self
        if not self.completed_at:
            raise ValueError("completed_at is required for terminal eval run states")
        if self.status == "completed" and self.result_status == "running":
            raise ValueError("completed eval runs must have a final result_status")
        if self.status == "failed":
            if self.result_status != "failed":
                raise ValueError("failed eval runs must have failed result_status")
            if self.error_json is None:
                raise ValueError("error_json is required for failed eval runs")
        review = self.gate_result.review_decision
        if review is None and self.result_status == "passed_with_notes":
            raise ValueError("passed_with_notes requires an audited EvalRun review decision")
        if review is not None:
            expected_result = "failed" if self.gate_result.blocked_dataset_case_ids else "passed_with_notes"
            if self.status != "completed" or self.result_status != expected_result:
                raise ValueError("audited EvalRun review decision does not match terminal result")
        return self

    def transition_to(
        self,
        status: str,
        *,
        fields: JsonObject | None = None,
    ) -> EvalRunRecord:
        validate_transition("eval_run", self.status, status)
        payload = self.to_payload()
        payload.update(fields or {})
        payload["status"] = status
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    def to_response(self, *, items: list[JsonObject]) -> JsonObject:
        payload = self.to_payload()
        payload["items"] = items
        return payload

    @classmethod
    def from_payload(cls, boundary_snapshot: JsonObject) -> EvalRunRecord:
        normalized = dict(boundary_snapshot)
        normalized.pop("items", None)
        return cls.model_validate(normalized)

    @classmethod
    def from_row(cls, row: EvalRunModel) -> EvalRunRecord:
        payload = dict(row.payload_json or {})
        payload.pop("items", None)
        payload.update(
            {
                "eval_run_id": row.eval_run_id,
                "dataset_id": row.dataset_id,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "status": row.status,
                "agent_id": row.agent_id or "main-agent",
                "agent_version_id": row.agent_version_id,
                "source": row.source,
            }
        )
        return cls.model_validate(payload)


class EvalRunItemRecord(StrictRuntimeRecord):
    """Internal source of truth for one persisted eval run item."""

    eval_run_item_id: str
    eval_run_id: str
    dataset_case_id: str
    agent_run_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    status: EvalRunItemStatus
    score: Optional[float] = None
    check_results: list[EvalRunCheckResultRecord] = Field(default_factory=list)
    dataset_case_snapshot: TestCaseRecord
    answer_summary: Optional[str] = None
    error_json: Optional[JsonObject] = None
    created_at: str

    @model_validator(mode="after")
    def validate_result_shape(self) -> EvalRunItemRecord:
        if self.dataset_case_snapshot.case_id != self.dataset_case_id:
            raise ValueError("dataset_case_snapshot must match dataset_case_id")
        if self.score is not None and not 0 <= self.score <= 1:
            raise ValueError("eval run item score must be between 0 and 1")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: EvalRunItemModel) -> EvalRunItemRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "eval_run_item_id": row.eval_run_item_id,
                "eval_run_id": row.eval_run_id,
                "dataset_case_id": row.dataset_case_id,
                "agent_run_id": row.agent_run_id,
                "status": row.status,
                "score": row.score,
            }
        )
        return cls.model_validate(payload)


class EvalRunProjectionRecord(EvalRunRecord):
    """Eval run snapshot after store projection attaches item payloads."""

    model_config = ConfigDict(extra="forbid")

    items: list[EvalRunItemRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_decision_evidence(self) -> EvalRunProjectionRecord:
        review = self.gate_result.review_decision
        if review is None:
            return self
        review_item_ids = {item.dataset_case_id for item in self.items if item.status == "needs_human_review"}
        decision_ids = {item.dataset_case_id for item in review.items}
        if decision_ids != review_item_ids:
            raise ValueError("EvalRun review must cover exactly the needs_human_review items")
        reviewed_items = [item for item in self.items if item.dataset_case_id in decision_ids]
        if any(not check.passed for item in reviewed_items for check in item.check_results if check.required):
            raise ValueError("EvalRun review cannot override a failed required check")
        return self
