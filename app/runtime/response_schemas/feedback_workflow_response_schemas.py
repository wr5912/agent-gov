from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.runtime.response_schemas.agent_version_response_schemas import AgentVersionDiffResponse, AgentVersionSummaryResponse
from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.response_schemas.error_response_schemas import FeedbackJobErrorResponse
from app.runtime.response_schemas.feedback_output_response_schemas import EvidenceRefResponse
from app.runtime.feedback_schemas import Actionability
from app.runtime.response_schemas.feedback_plan_response_schemas import (
    FeedbackOptimizationPlanResponse,
    FeedbackOptimizationPlanTaskResponse,
)
from app.runtime.schemas import (
    EvalRunResponse,
    ExtensibleResponse,
    FeedbackEvalCaseGenerateResponse,
    FeedbackSourceRef,
    RegressionGateOverrideResponse,
    RegressionImpactAnalysisResponse,
    RegressionPlanResponse,
)


class OptimizationTaskProposalResponse(ExtensibleResponse):
    proposal_id: Optional[str] = None
    feedback_case_id: Optional[str] = None
    proposal_job_id: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    actionability: Optional[Actionability] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    objective: Optional[str] = None
    recommendation: Optional[str] = None
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    requires_approval: Optional[bool] = None
    base_agent_version_id: Optional[str] = None


class OptimizationExecutionPlanOperationResponse(ExtensibleResponse):
    operation: Optional[str] = None
    path: Optional[str] = None
    append_text: Optional[str] = None
    content: Optional[str] = None
    expected_sha256: Optional[str] = None
    rationale: Optional[str] = None


class OptimizationExecutionPlannedDiffFileResponse(ExtensibleResponse):
    path: str
    operation: str
    status: str
    expected_sha256: Optional[str] = None
    before_sha256: Optional[str] = None
    after_sha256: Optional[str] = None
    unified_diff: str = ""
    is_text: bool = True
    truncated: bool = False
    reason: Optional[str] = None
    rationale: Optional[str] = None


class OptimizationExecutionPlannedDiffResponse(ExtensibleResponse):
    schema_version: str = "execution-planned-diff/v1"
    files: list[OptimizationExecutionPlannedDiffFileResponse] = Field(default_factory=list)
    added: int = 0
    modified: int = 0
    deleted: int = 0
    unchanged: int = 0
    noop: int = 0


class OptimizationExecutionPlanOutputResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    optimization_task_id: Optional[str] = None
    execution_job_id: Optional[str] = None
    status: Optional[str] = None
    baseline_agent_version_id: Optional[str] = None
    summary: Optional[str] = None
    operations: list[OptimizationExecutionPlanOperationResponse] = Field(default_factory=list)
    planned_diff: Optional[OptimizationExecutionPlannedDiffResponse] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    human_review_required: Optional[bool] = None
    no_action_reason: Optional[str] = None


class ExecutionCompensationResponse(ExtensibleResponse):
    schema_version: str = "execution-compensation/v1"
    compensation_id: str
    created_at: str
    updated_at: str
    status: str
    compensation_type: str
    optimization_task_id: str
    execution_job_id: str
    pre_execution_agent_version_id: Optional[str] = None
    restore_status: str
    original_error: str
    restore_error: Optional[str] = None
    manual_restore_result: JsonObject = Field(default_factory=dict)


class ExecutionApplicationResponse(ExtensibleResponse):
    schema_version: str = "execution-application/v1"
    application_id: str
    execution_job_id: str
    optimization_task_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: str
    pre_execution_agent_version_id: Optional[str] = None
    pre_execution_agent_version: Optional[AgentVersionSummaryResponse] = None
    applied_agent_version_id: Optional[str] = None
    applied_agent_version: Optional[AgentVersionSummaryResponse] = None
    applied_diff: Optional[AgentVersionDiffResponse] = None
    error_json: Optional[FeedbackJobErrorResponse] = None


class OptimizationExecutionJobResponse(ExtensibleResponse):
    execution_job_id: str
    optimization_task_id: str
    feedback_case_id: Optional[str] = None
    proposal_id: Optional[str] = None
    status: str
    profile_name: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    baseline_agent_version_id: Optional[str] = None
    input_path: Optional[str] = None
    input_json: Optional[JsonObject] = None
    raw_output_json: Optional[JsonObject] = None
    validated_output_json: Optional[OptimizationExecutionPlanOutputResponse] = None
    error_json: Optional[FeedbackJobErrorResponse] = None
    profile_version: Optional[JsonObject] = None
    compensations: list[ExecutionCompensationResponse] = Field(default_factory=list)


class OptimizationTaskResponse(ExtensibleResponse):
    optimization_task_id: str
    created_at: str
    status: str
    proposal_id: Optional[str] = None
    proposal_ids: list[str] = Field(default_factory=list)
    feedback_case_id: Optional[str] = None
    execution_mode: str
    source: str
    comment: Optional[str] = None
    target_paths: list[str] = Field(default_factory=list)
    proposal: Optional[OptimizationTaskProposalResponse] = None
    baseline_agent_version_id: Optional[str] = None
    execution_job_ids: list[str] = Field(default_factory=list)
    latest_execution_job_id: Optional[str] = None
    latest_execution_job: Optional[OptimizationExecutionJobResponse] = None
    pre_execution_agent_version_id: Optional[str] = None
    pre_execution_agent_version: Optional[AgentVersionSummaryResponse] = None
    applied_at: Optional[str] = None
    applied_agent_version_id: Optional[str] = None
    applied_agent_version: Optional[AgentVersionSummaryResponse] = None
    regression_run_ids: list[str] = Field(default_factory=list)
    latest_regression_run_id: Optional[str] = None
    latest_regression_run: Optional[EvalRunResponse] = None
    regression_completed_at: Optional[str] = None


class OptimizationExecutionApplyResponse(BaseModel):
    execution_job: OptimizationExecutionJobResponse
    execution_application: ExecutionApplicationResponse
    optimization_task: OptimizationTaskResponse
    applied_diff: Optional[AgentVersionDiffResponse] = None


class ExternalGovernanceWebhookResponse(BaseModel):
    alias: str
    name: str
    url: str
    has_token: bool = False


class ExternalGovernanceNotificationResponse(BaseModel):
    notification_id: str
    external_item_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: str
    webhook_alias: str
    http_status: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None
    request_json: JsonObject = Field(default_factory=dict)


class ExternalGovernanceItemResponse(ExtensibleResponse):
    schema_version: str = "external-governance-item/v1"
    external_item_id: str
    created_at: str
    updated_at: str
    status: str
    feedback_case_id: str
    proposal_job_id: str
    source_index: int = 0
    owner: str
    actionability: str
    title: Optional[str] = None
    description: Optional[str] = None
    objective: Optional[str] = None
    target_summary: Optional[str] = None
    task_context: JsonObject = Field(default_factory=dict)
    recommendation: str
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[EvidenceRefResponse] = Field(default_factory=list)
    reason: Optional[str] = None
    source: Optional[str] = None
    batch_id: Optional[str] = None
    optimization_plan_id: Optional[str] = None
    plan_task_id: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    source_attribution_job_ids: list[str] = Field(default_factory=list)
    latest_notification_id: Optional[str] = None
    latest_webhook_alias: Optional[str] = None
    latest_notification: Optional[ExternalGovernanceNotificationResponse] = None
    superseded_at: Optional[str] = None
    superseded_reason: Optional[str] = None
    superseded_by_job_id: Optional[str] = None


class FeedbackOptimizationSkippedSourceRefResponse(ExtensibleResponse):
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    reason: Optional[str] = None


class FeedbackOptimizationBatchAttributionSummaryResponse(ExtensibleResponse):
    total: int = 0
    completed: int = 0
    running: int = 0
    needs_review_or_failed: int = 0


class FeedbackOptimizationBatchResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    batch_id: str
    created_at: str
    updated_at: str
    status: str
    title: str
    priority: Optional[str] = None
    source_refs: list[FeedbackSourceRef] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    skipped_source_refs: list[FeedbackOptimizationSkippedSourceRefResponse] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    eval_case_generation: Optional[FeedbackEvalCaseGenerateResponse] = None
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_jobs: list[AgentJobResponse] = Field(default_factory=list)
    attribution_summary: FeedbackOptimizationBatchAttributionSummaryResponse = Field(
        default_factory=FeedbackOptimizationBatchAttributionSummaryResponse
    )
    optimization_plan: Optional[FeedbackOptimizationPlanResponse] = None
    optimization_plan_job_id: Optional[str] = None
    optimization_plan_job: Optional[AgentJobResponse] = None
    optimization_plan_error: Optional[FeedbackJobErrorResponse] = None
    internal_proposal_id: Optional[str] = None
    optimization_task_id: Optional[str] = None
    optimization_task: Optional[OptimizationTaskResponse] = None
    execution_job_id: Optional[str] = None
    execution_job: Optional[OptimizationExecutionJobResponse] = None
    eval_run_id: Optional[str] = None
    latest_eval_run: Optional[EvalRunResponse] = None
    regression_plan_id: Optional[str] = None
    latest_regression_plan: Optional[RegressionPlanResponse] = None
    latest_regression_gate: JsonObject = Field(default_factory=dict)
    execution_apply_result: Optional[OptimizationExecutionApplyResponse] = None


class FeedbackOptimizationBatchAttributionResponse(BaseModel):
    batch: Optional[FeedbackOptimizationBatchResponse] = None
    jobs: list[AgentJobResponse] = Field(default_factory=list)


class FeedbackOptimizationBatchExecutionResponse(BaseModel):
    batch: Optional[FeedbackOptimizationBatchResponse] = None
    optimization_task: Optional[OptimizationTaskResponse] = None
    execution_job: Optional[OptimizationExecutionJobResponse] = None
    apply_result: Optional[OptimizationExecutionApplyResponse] = None


class FeedbackOptimizationPlanTaskExecuteResponse(BaseModel):
    batch: Optional[FeedbackOptimizationBatchResponse] = None
    plan_task: Optional[FeedbackOptimizationPlanTaskResponse] = None
    optimization_task: Optional[OptimizationTaskResponse] = None
    execution_job: Optional[OptimizationExecutionJobResponse] = None
    apply_result: Optional[OptimizationExecutionApplyResponse] = None
    external_item: Optional[ExternalGovernanceItemResponse] = None


class FeedbackOptimizationBatchRegressionResponse(BaseModel):
    batch: Optional[FeedbackOptimizationBatchResponse] = None
    eval_run: EvalRunResponse
    regression_plan: Optional[RegressionPlanResponse] = None
    impact_analysis: Optional[RegressionImpactAnalysisResponse] = None
    impact_analysis_job: Optional[AgentJobResponse] = None
    gate_override: Optional[RegressionGateOverrideResponse] = None
