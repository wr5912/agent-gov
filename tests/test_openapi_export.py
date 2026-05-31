import json
import os
import subprocess
import sys
from pathlib import Path


def test_export_openapi_script_writes_schema(tmp_path):
    root = tmp_path / "docker" / "volume"
    env = os.environ.copy()
    env.update(
        {
            "WORKSPACE_DIR": str(root / "main-workspace"),
            "MAIN_WORKSPACE_DIR": str(root / "main-workspace"),
            "ATTRIBUTION_ANALYZER_WORKSPACE_DIR": str(root / "attribution-analyzer-workspace"),
            "PROPOSAL_GENERATOR_WORKSPACE_DIR": str(root / "proposal-generator-workspace"),
            "EXECUTION_OPTIMIZER_WORKSPACE_DIR": str(root / "execution-optimizer-workspace"),
            "DATA_DIR": str(root / "data"),
            "CLAUDE_ROOT": str(root / "claude-roots" / "main"),
            "MAIN_CLAUDE_ROOT": str(root / "claude-roots" / "main"),
            "ATTRIBUTION_ANALYZER_CLAUDE_ROOT": str(root / "claude-roots" / "attribution-analyzer"),
            "PROPOSAL_GENERATOR_CLAUDE_ROOT": str(root / "claude-roots" / "proposal-generator"),
            "EXECUTION_OPTIMIZER_CLAUDE_ROOT": str(root / "claude-roots" / "execution-optimizer"),
            "CLAUDE_HOME": str(root / "claude-roots" / "main" / ".claude"),
            "ANTHROPIC_API_KEY": "",
            "MODEL_PROVIDER_API_KEY": "",
            "API_KEY": "",
        }
    )
    output_path = tmp_path / "openapi.json"

    subprocess.run(
        [sys.executable, "scripts/export_openapi.py", "--output", str(output_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
    )

    schema = json.loads(output_path.read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.")
    assert "/api/feedback-signals" in schema["paths"]
    assert "/api/feedback-optimization-batches" in schema["paths"]
    assert_schema_ref(schema, "/health", "get", "RuntimeHealthResponse")
    assert_schema_ref(schema, "/api/evidence-packages/{evidence_package_id}", "get", "EvidencePackageResponse")
    assert_schema_ref(
        schema,
        "/api/evidence-packages/{evidence_package_id}/files/{file_name}",
        "get",
        "EvidencePackageFileResponse",
    )
    assert_schema_ref(schema, "/api/agent-versions/main/current", "get", "AgentVersionSummaryResponse")
    assert_schema_ref(schema, "/api/agent-versions/main", "get", "AgentVersionSummaryResponse", array=True)
    assert_schema_ref(schema, "/api/agent-versions/main/snapshots", "post", "AgentVersionSummaryResponse")
    assert_schema_ref(schema, "/api/agent-versions/main/{version_id}", "get", "AgentVersionManifestResponse")
    assert_schema_ref(schema, "/api/agent-versions/main/diff", "get", "AgentVersionDiffResponse")
    assert_schema_ref(schema, "/api/agent-versions/main/file-diff", "get", "AgentVersionFileDiffResponse")
    assert_schema_ref(schema, "/api/agent-versions/main/{version_id}/rollback", "post", "AgentVersionRestoreResponse")
    assert_schema_ref(schema, "/api/soc-events", "get", "SocEventResponse", array=True)
    assert_schema_ref(schema, "/api/pending-correlations", "get", "PendingCorrelationResponse", array=True)
    assert_schema_ref(schema, "/api/feedback-sources", "get", "FeedbackSourceResponse", array=True)
    assert_schema_ref(schema, "/api/feedback-sources/eval-cases/generate", "post", "FeedbackEvalCaseGenerateResponse")
    assert_schema_ref(
        schema,
        "/api/feedback-analysis/jobs/{job_id}/attribution",
        "get",
        "AttributionOutputResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-analysis/jobs/{job_id}/proposal",
        "get",
        "ProposalOutputResponse",
    )
    assert_schema_ref(schema, "/api/eval-cases", "get", "EvalCaseResponse", array=True)
    assert_schema_ref(schema, "/api/eval-runs", "get", "EvalRunResponse", array=True)
    assert_schema_ref(schema, "/api/feedback-optimization-batches", "get", "FeedbackOptimizationBatchResponse", array=True)
    assert_schema_ref(schema, "/api/feedback-optimization-batches", "post", "FeedbackOptimizationBatchResponse")
    assert_schema_ref(schema, "/api/feedback-optimization-batches/{batch_id}", "get", "FeedbackOptimizationBatchResponse")
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/attribution-jobs",
        "post",
        "FeedbackOptimizationBatchAttributionResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/approve",
        "post",
        "FeedbackOptimizationBatchExecutionResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}/execute",
        "post",
        "FeedbackOptimizationPlanTaskExecuteResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/regression-runs",
        "post",
        "FeedbackOptimizationBatchRegressionResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/eval-cases",
        "get",
        "EvalCaseResponse",
        array=True,
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/eval-cases",
        "post",
        "EvalCaseResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/eval-cases/{eval_case_id}",
        "patch",
        "EvalCaseResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/eval-cases/{eval_case_id}",
        "delete",
        "FeedbackOptimizationBatchResponse",
    )
    assert_schema_ref(
        schema,
        "/api/optimization-tasks/{task_id}/execution-jobs",
        "post",
        "OptimizationExecutionJobResponse",
    )
    assert_schema_ref(
        schema,
        "/api/optimization-tasks/{task_id}/execution-jobs",
        "get",
        "OptimizationExecutionJobResponse",
        array=True,
    )
    assert_schema_ref(
        schema,
        "/api/optimization-tasks/{task_id}/execution-jobs/{execution_job_id}/apply",
        "post",
        "OptimizationExecutionApplyResponse",
    )
    assert_schema_ref(schema, "/api/execution-compensations", "get", "ExecutionCompensationResponse", array=True)
    assert_schema_ref(
        schema,
        "/api/execution-compensations/{compensation_id}",
        "get",
        "ExecutionCompensationResponse",
    )
    assert_schema_ref(
        schema,
        "/api/execution-compensations/{compensation_id}/restore",
        "post",
        "ExecutionCompensationResponse",
    )
    assert_schema_ref(schema, "/api/optimization-tasks/{task_id}/regression-runs", "post", "EvalRunResponse")
    assert_schema_ref(
        schema,
        "/api/optimization-tasks/{task_id}/regression-runs",
        "get",
        "EvalRunResponse",
        array=True,
    )
    assert_schema_ref(schema, "/api/optimization-proposals", "get", "OptimizationProposalResponse", array=True)
    assert_schema_ref(schema, "/api/optimization-proposals/{proposal_id}", "get", "OptimizationProposalResponse")
    assert_schema_ref(
        schema,
        "/api/optimization-proposals/{proposal_id}/approve",
        "post",
        "OptimizationProposalReviewResponse",
    )
    assert_schema_ref(
        schema,
        "/api/optimization-proposals/{proposal_id}/reject",
        "post",
        "OptimizationProposalReviewResponse",
    )
    assert_schema_ref(
        schema,
        "/api/optimization-proposals/{proposal_id}/request-more-analysis",
        "post",
        "OptimizationProposalReviewResponse",
    )
    assert_schema_ref(schema, "/api/external-governance-webhooks", "get", "ExternalGovernanceWebhookResponse", array=True)
    assert_schema_ref(schema, "/api/external-governance-items", "get", "ExternalGovernanceItemResponse", array=True)
    assert_schema_ref(
        schema,
        "/api/external-governance-items/{external_item_id}/notify",
        "post",
        "ExternalGovernanceItemResponse",
    )
    plan_schema = schema["components"]["schemas"]["FeedbackOptimizationPlanResponse"]
    assert plan_schema["properties"]["blocked_items"]["items"] == {
        "$ref": "#/components/schemas/FeedbackOptimizationBlockedItemResponse"
    }
    assert plan_schema["properties"]["source_refs"]["items"] == {"$ref": "#/components/schemas/FeedbackSourceRef"}
    assert plan_schema["properties"]["evidence_refs"]["items"] == {
        "$ref": "#/components/schemas/EvidenceRefResponse"
    }
    assert plan_schema["properties"]["attribution_summaries"]["items"] == {
        "$ref": "#/components/schemas/FeedbackOptimizationAttributionSummaryResponse"
    }
    plan_task_schema = schema["components"]["schemas"]["FeedbackOptimizationPlanTaskResponse"]
    assert plan_task_schema["properties"]["task_context"] == {
        "$ref": "#/components/schemas/FeedbackOptimizationTaskContextResponse"
    }
    assert plan_task_schema["properties"]["evidence_refs"]["items"] == {
        "$ref": "#/components/schemas/EvidenceRefResponse"
    }
    execution_schema = schema["components"]["schemas"]["OptimizationExecutionJobResponse"]
    assert execution_schema["properties"]["validated_output_json"]["anyOf"][0] == {
        "$ref": "#/components/schemas/OptimizationExecutionPlanOutputResponse"
    }
    assert_nullable_schema_ref(execution_schema, "pre_execution_agent_version", "AgentVersionSummaryResponse")
    assert_nullable_schema_ref(execution_schema, "applied_agent_version", "AgentVersionSummaryResponse")
    assert_nullable_schema_ref(execution_schema, "applied_diff", "AgentVersionDiffResponse")
    assert_nullable_schema_ref(execution_schema, "error_json", "FeedbackJobErrorResponse")
    assert execution_schema["properties"]["compensations"]["items"] == {"$ref": "#/components/schemas/ExecutionCompensationResponse"}
    task_schema = schema["components"]["schemas"]["OptimizationTaskResponse"]
    assert_nullable_schema_ref(task_schema, "proposal", "OptimizationTaskProposalResponse")
    assert_nullable_schema_ref(task_schema, "latest_execution_job", "OptimizationExecutionJobResponse")
    assert_nullable_schema_ref(task_schema, "pre_execution_agent_version", "AgentVersionSummaryResponse")
    assert_nullable_schema_ref(task_schema, "applied_agent_version", "AgentVersionSummaryResponse")
    assert_nullable_schema_ref(task_schema, "latest_regression_run", "EvalRunResponse")
    batch_schema = schema["components"]["schemas"]["FeedbackOptimizationBatchResponse"]
    assert_nullable_schema_ref(batch_schema, "optimization_task", "OptimizationTaskResponse")
    assert_nullable_schema_ref(batch_schema, "execution_job", "OptimizationExecutionJobResponse")
    assert_nullable_schema_ref(batch_schema, "execution_apply_result", "OptimizationExecutionApplyResponse")
    assert batch_schema["properties"]["skipped_source_refs"]["items"] == {
        "$ref": "#/components/schemas/FeedbackOptimizationSkippedSourceRefResponse"
    }
    assert batch_schema["properties"]["attribution_summary"] == {
        "$ref": "#/components/schemas/FeedbackOptimizationBatchAttributionSummaryResponse"
    }
    assert_nullable_schema_ref(batch_schema, "optimization_plan_error", "FeedbackJobErrorResponse")
    batch_execution_schema = schema["components"]["schemas"]["FeedbackOptimizationBatchExecutionResponse"]
    assert_nullable_schema_ref(batch_execution_schema, "optimization_task", "OptimizationTaskResponse")
    assert_nullable_schema_ref(batch_execution_schema, "execution_job", "OptimizationExecutionJobResponse")
    assert_nullable_schema_ref(batch_execution_schema, "apply_result", "OptimizationExecutionApplyResponse")
    apply_schema = schema["components"]["schemas"]["OptimizationExecutionApplyResponse"]
    assert_nullable_schema_ref(apply_schema, "applied_diff", "AgentVersionDiffResponse")
    plan_task_execution_schema = schema["components"]["schemas"]["FeedbackOptimizationPlanTaskExecuteResponse"]
    assert_nullable_schema_ref(plan_task_execution_schema, "optimization_task", "OptimizationTaskResponse")
    assert_nullable_schema_ref(plan_task_execution_schema, "execution_job", "OptimizationExecutionJobResponse")
    assert_nullable_schema_ref(plan_task_execution_schema, "apply_result", "OptimizationExecutionApplyResponse")
    analysis_job_schema = schema["components"]["schemas"]["FeedbackAnalysisJobResponse"]
    validated_output_schema_options = analysis_job_schema["properties"]["validated_output_json"]["anyOf"]
    for schema_name in ("AttributionOutputResponse", "ProposalOutputResponse", "FeedbackOptimizationPlanResponse"):
        assert {"$ref": f"#/components/schemas/{schema_name}"} in validated_output_schema_options
    assert {"type": "null"} in validated_output_schema_options
    assert_nullable_schema_ref(analysis_job_schema, "error_json", "FeedbackJobErrorResponse")
    restore_schema = schema["components"]["schemas"]["AgentVersionRestoreResponse"]
    assert restore_schema["properties"]["current_version"] == {
        "$ref": "#/components/schemas/AgentVersionSummaryResponse"
    }
    manifest_schema = schema["components"]["schemas"]["AgentVersionManifestResponse"]
    assert manifest_schema["properties"]["included_roots"]["items"] == {
        "$ref": "#/components/schemas/AgentVersionIncludedRootResponse"
    }
    assert manifest_schema["properties"]["excluded_paths"]["items"] == {
        "$ref": "#/components/schemas/AgentVersionExcludedPathResponse"
    }
    assert manifest_schema["properties"]["skipped_paths"]["items"] == {
        "$ref": "#/components/schemas/AgentVersionSkippedPathResponse"
    }
    assert manifest_schema["properties"]["related_data"] == {
        "$ref": "#/components/schemas/AgentVersionRelatedDataResponse"
    }
    review_schema = schema["components"]["schemas"]["OptimizationProposalReviewResponse"]
    assert review_schema["properties"]["proposal"] == {
        "$ref": "#/components/schemas/OptimizationProposalResponse"
    }
    proposal_output_schema = schema["components"]["schemas"]["ProposalOutputResponse"]
    assert proposal_output_schema["properties"]["external_guidance"]["items"] == {
        "$ref": "#/components/schemas/ExternalGuidanceResponse"
    }
    evidence_schema = schema["components"]["schemas"]["EvidencePackageResponse"]
    assert evidence_schema["properties"]["source_refs"] == {"$ref": "#/components/schemas/EvidenceSourceRefsResponse"}
    assert evidence_schema["properties"]["included_files"]["items"] == {
        "$ref": "#/components/schemas/EvidenceIncludedFileResponse"
    }
    assert evidence_schema["properties"]["redaction"] == {"$ref": "#/components/schemas/EvidenceRedactionResponse"}
    assert evidence_schema["properties"]["completeness"] == {"$ref": "#/components/schemas/EvidenceCompletenessResponse"}
    eval_case_schema = schema["components"]["schemas"]["EvalCaseResponse"]
    assert_nullable_schema_ref(eval_case_schema, "source_summary", "EvalCaseSourceSummaryResponse")
    assert_nullable_schema_ref(eval_case_schema, "attribution_summary", "EvalCaseAttributionSummaryResponse")
    assert_nullable_schema_ref(eval_case_schema, "proposal_summary", "EvalCaseProposalSummaryResponse")
    eval_generate_schema = schema["components"]["schemas"]["FeedbackEvalCaseGenerateResponse"]
    assert eval_generate_schema["properties"]["results"]["items"] == {
        "$ref": "#/components/schemas/FeedbackEvalCaseGenerateResultResponse"
    }
    eval_item_schema = schema["components"]["schemas"]["EvalRunItemResponse"]
    assert eval_item_schema["properties"]["check_results"]["items"] == {
        "$ref": "#/components/schemas/EvalRunCheckResultResponse"
    }
    eval_run_schema = schema["components"]["schemas"]["EvalRunResponse"]
    assert eval_run_schema["properties"]["summary"] == {"$ref": "#/components/schemas/EvalRunSummaryResponse"}
    assert_nullable_schema_ref(eval_run_schema, "error_json", "FeedbackJobErrorResponse")
    assert_nullable_schema_ref(eval_item_schema, "error_json", "FeedbackJobErrorResponse")
    external_item_schema = schema["components"]["schemas"]["ExternalGovernanceItemResponse"]
    for field_name in (
        "title",
        "description",
        "objective",
        "target_summary",
        "task_context",
        "acceptance_criteria",
        "target_type",
        "target_path",
        "plan_task_id",
        "latest_webhook_alias",
        "schema_version",
        "superseded_at",
        "superseded_reason",
        "superseded_by_job_id",
    ):
        assert field_name in external_item_schema["properties"]


def assert_schema_ref(schema, path, method, schema_name, *, array=False):
    response_schema = schema["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
    target = response_schema["items"] if array else response_schema
    assert target == {"$ref": f"#/components/schemas/{schema_name}"}


def assert_nullable_schema_ref(schema, field_name, schema_name):
    assert schema["properties"][field_name]["anyOf"][0] == {"$ref": f"#/components/schemas/{schema_name}"}
    assert schema["properties"][field_name]["anyOf"][1] == {"type": "null"}
