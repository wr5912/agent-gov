import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.export_openapi import CONTAINER_RUNTIME_VOLUME_ROOT, LOCAL_DEBUG_RUNTIME_VOLUME_ROOT, _apply_local_defaults, _local_default_volume_root


def test_export_openapi_local_defaults_use_debug_volume_unless_container_mode(monkeypatch):
    monkeypatch.delenv("HOST_RUNTIME_VOLUME_ROOT", raising=False)
    monkeypatch.delenv("RUNTIME_VOLUME_MODE", raising=False)
    monkeypatch.delenv("RUNTIME_CONTAINER", raising=False)

    assert _local_default_volume_root() == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT

    monkeypatch.setenv("RUNTIME_CONTAINER", "1")
    assert _local_default_volume_root() == CONTAINER_RUNTIME_VOLUME_ROOT

    monkeypatch.setenv("HOST_RUNTIME_VOLUME_ROOT", "/tmp/custom-runtime-root")
    assert _local_default_volume_root() == Path("/tmp/custom-runtime-root")


def test_export_openapi_applies_local_debug_env_file_mode(monkeypatch):
    original = os.environ.copy()
    for key in (
        "RUNTIME_VOLUME_MODE",
        "RUNTIME_CONTAINER",
        "HOST_RUNTIME_VOLUME_ROOT",
        "WORKSPACE_DIR",
        "MAIN_WORKSPACE_DIR",
        "GOVERNOR_WORKSPACE_DIR",
        "DATA_DIR",
        "CLAUDE_ROOT",
        "MAIN_CLAUDE_ROOT",
        "GOVERNOR_CLAUDE_ROOT",
        "CLAUDE_HOME",
    ):
        monkeypatch.delenv(key, raising=False)

    try:
        _apply_local_defaults(Path.cwd())

        assert "RUNTIME_VOLUME_MODE" not in os.environ
        assert os.environ["RUNTIME_CONTAINER"] == "0"
        assert os.environ["HOST_RUNTIME_VOLUME_ROOT"] == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT.as_posix()
        assert os.environ["WORKSPACE_DIR"] == (LOCAL_DEBUG_RUNTIME_VOLUME_ROOT / "main-workspace").as_posix()
        assert os.environ["DATA_DIR"] == (LOCAL_DEBUG_RUNTIME_VOLUME_ROOT / "data").as_posix()
        assert os.environ["CLAUDE_ROOT"] == (LOCAL_DEBUG_RUNTIME_VOLUME_ROOT / "claude-roots" / "main").as_posix()
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_export_openapi_script_writes_schema(tmp_path):
    root = tmp_path / "docker" / "volume"
    env = os.environ.copy()
    env.update(
        {
            "WORKSPACE_DIR": str(root / "main-workspace"),
            "MAIN_WORKSPACE_DIR": str(root / "main-workspace"),
            "GOVERNOR_WORKSPACE_DIR": str(root / "governor-workspace"),
            "DATA_DIR": str(root / "data"),
            "CLAUDE_ROOT": str(root / "claude-roots" / "main"),
            "MAIN_CLAUDE_ROOT": str(root / "claude-roots" / "main"),
            "GOVERNOR_CLAUDE_ROOT": str(root / "claude-roots" / "governor"),
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
    assert "/v1/chat/completions" in schema["paths"]
    assert "/v1/responses" not in schema["paths"]
    assert "/api/claude-user-input-requests" in schema["paths"]
    assert "/api/claude-user-input-requests/{request_id}/decision" in schema["paths"]
    assert "/api/claude-hitl-requests" not in schema["paths"]
    assert "/api/claude-hitl-requests/{request_id}/decision" not in schema["paths"]
    assert_schema_ref(schema, "/health", "get", "RuntimeHealthResponse")
    assert_schema_ref(schema, "/api/evidence-packages/{evidence_package_id}", "get", "EvidencePackageResponse")
    assert_schema_ref(
        schema,
        "/api/evidence-packages/{evidence_package_id}/files/{file_name}",
        "get",
        "EvidencePackageFileResponse",
    )
    assert "/api/agent-versions/main/current" not in schema["paths"]
    assert_schema_ref(schema, "/api/agent-repository", "get", "AgentRepositoryStatusResponse")
    assert_schema_ref(schema, "/api/agent-repository/current", "get", "AgentGitRefResponse")
    assert_schema_ref(schema, "/api/agent-change-sets", "get", "AgentChangeSetResponse", array=True)
    assert_schema_ref(schema, "/api/agent-change-sets", "post", "AgentChangeSetResponse")
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}", "get", "AgentChangeSetResponse")
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}/events", "get", "AgentChangeSetEventResponse", array=True)
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}/diff", "get", "AgentGitDiffResponse")
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}/file-diff", "get", "AgentGitFileDiffResponse")
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}/approve", "post", "AgentChangeSetResponse")
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}/reject", "post", "AgentChangeSetResponse")
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}/regression-runs", "post", "EvalRunResponse")
    assert_schema_ref(schema, "/api/agent-change-sets/{change_set_id}/publish", "post", "AgentReleaseResponse")
    assert_schema_ref(schema, "/api/agent-releases", "get", "AgentReleaseResponse", array=True)
    assert_schema_ref(schema, "/api/agent-releases/{release_id}/restore", "post", "AgentReleaseRestoreResponse")
    assert_schema_ref(schema, "/api/agent-releases/{release_id}/rollback", "post", "AgentReleaseResponse")
    assert_schema_ref(schema, "/api/soc-events", "get", "SocEventResponse", array=True)
    assert_schema_ref(schema, "/api/pending-correlations", "get", "PendingCorrelationResponse", array=True)
    assert_schema_ref(schema, "/api/feedback-sources", "get", "FeedbackSourceResponse", array=True)
    assert_schema_ref(schema, "/api/agent-jobs", "get", "AgentJobResponse", array=True)
    assert_schema_ref(schema, "/api/agent-jobs/{job_id}", "get", "AgentJobResponse")
    assert_schema_ref(schema, "/api/feedback-sources/eval-cases/generate", "post", "AgentJobResponse")
    assert "/api/feedback-analysis/jobs/{job_id}" not in schema["paths"]
    assert "/api/feedback-analysis/jobs/{job_id}/attribution" not in schema["paths"]
    assert "/api/feedback-analysis/jobs/{job_id}/proposal" not in schema["paths"]
    assert_schema_ref(schema, "/api/eval-cases", "get", "EvalCaseResponse", array=True)
    assert_schema_ref(schema, "/api/eval-runs", "get", "EvalRunResponse", array=True)
    assert_schema_ref(schema, "/api/regression-assets", "get", "EvalCaseResponse", array=True)
    assert_schema_ref(schema, "/api/regression-assets/{eval_case_id}/promote", "post", "EvalCaseResponse")
    assert_schema_ref(schema, "/api/regression-assets/{eval_case_id}/revisions", "get", "EvalCaseRevisionResponse", array=True)
    assert_schema_ref(
        schema,
        "/api/regression-assets/{eval_case_id}/governance-events",
        "get",
        "EvalCaseGovernanceEventResponse",
        array=True,
    )
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
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan",
        "post",
        "AgentJobResponse",
    )
    assert "/api/feedback-optimization-batches/{batch_id}/optimization-plan/approve" not in schema["paths"]
    assert "/api/feedback-optimization-batches/{batch_id}/optimization-plan/reject" not in schema["paths"]
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/execute-all",
        "post",
        "FeedbackOptimizationBatchExecuteAllResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/executions/{execution_run_id}/rollback",
        "post",
        "FeedbackOptimizationBatchExecutionRollbackResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}",
        "patch",
        "FeedbackOptimizationPlanTaskUpdateResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}/execute",
        "post",
        "FeedbackOptimizationPlanTaskExecuteResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/regression-plan",
        "post",
        "RegressionPlanResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/regression-plan",
        "get",
        "RegressionPlanResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/regression-runs",
        "post",
        "FeedbackOptimizationBatchRegressionResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/regression-runs/{eval_run_id}/impact-analysis",
        "post",
        "AgentJobResponse",
    )
    assert_schema_ref(
        schema,
        "/api/feedback-optimization-batches/{batch_id}/regression-runs/{eval_run_id}/gate-overrides",
        "post",
        "RegressionGateOverrideResponse",
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
        "/api/feedback-optimization-batches/{batch_id}/eval-cases/promote",
        "post",
        "FeedbackOptimizationBatchEvalCasePromotionResponse",
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
        "AgentJobResponse",
    )
    assert "get" not in schema["paths"]["/api/optimization-tasks/{task_id}/execution-jobs"]
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
    assert_schema_ref(
        schema,
        "/api/feedback-cases/{feedback_case_id}/optimization-plan",
        "post",
        "AgentJobResponse",
    )
    assert "/api/feedback-cases/{feedback_case_id}/proposal-jobs" not in schema["paths"]
    assert "/api/feedback-cases/{feedback_case_id}/proposal-jobs/regenerate" not in schema["paths"]
    assert "/api/optimization-proposals" not in schema["paths"]
    assert "/api/optimization-proposals/{proposal_id}" not in schema["paths"]
    assert "/api/optimization-proposals/{proposal_id}/approve" not in schema["paths"]
    assert "/api/optimization-proposals/{proposal_id}/reject" not in schema["paths"]
    assert "/api/optimization-proposals/{proposal_id}/request-more-analysis" not in schema["paths"]
    assert "/api/optimization-proposals/{proposal_id}/tasks" not in schema["paths"]
    assert_schema_ref(schema, "/api/external-governance-webhooks", "get", "ExternalGovernanceWebhookResponse", array=True)
    assert_schema_ref(schema, "/api/external-governance-items", "get", "ExternalGovernanceItemResponse", array=True)
    assert_schema_ref(
        schema,
        "/api/external-governance-items/{external_item_id}/notify",
        "post",
        "ExternalGovernanceItemResponse",
    )
    plan_schema = schema["components"]["schemas"]["FeedbackOptimizationPlanResponse"]
    assert plan_schema["properties"]["blocked_items"]["items"] == {"$ref": "#/components/schemas/FeedbackOptimizationBlockedItemResponse"}
    assert plan_schema["properties"]["source_refs"]["items"] == {"$ref": "#/components/schemas/FeedbackSourceRef"}
    assert plan_schema["properties"]["evidence_refs"]["items"] == {"$ref": "#/components/schemas/EvidenceRefResponse"}
    assert plan_schema["properties"]["attribution_summaries"]["items"] == {"$ref": "#/components/schemas/FeedbackOptimizationAttributionSummaryResponse"}
    assert plan_schema["properties"]["task_summary"] == {"$ref": "#/components/schemas/FeedbackOptimizationPlanTaskSummaryResponse"}
    assert plan_schema["properties"]["blocked_summary"] == {"$ref": "#/components/schemas/FeedbackOptimizationBlockedSummaryResponse"}
    plan_task_schema = schema["components"]["schemas"]["FeedbackOptimizationPlanTaskResponse"]
    assert plan_task_schema["properties"]["task_context"] == {"$ref": "#/components/schemas/FeedbackOptimizationTaskContextResponse"}
    assert plan_task_schema["properties"]["evidence_refs"]["items"] == {"$ref": "#/components/schemas/EvidenceRefResponse"}
    assert "FeedbackAnalysisJobResponse" not in schema["components"]["schemas"]
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
    assert batch_schema["properties"]["attribution_jobs"]["items"] == {"$ref": "#/components/schemas/AgentJobResponse"}
    assert_nullable_schema_ref(batch_schema, "optimization_plan_job", "AgentJobResponse")
    assert batch_schema["properties"]["skipped_source_refs"]["items"] == {"$ref": "#/components/schemas/FeedbackOptimizationSkippedSourceRefResponse"}
    assert batch_schema["properties"]["attribution_summary"] == {"$ref": "#/components/schemas/FeedbackOptimizationBatchAttributionSummaryResponse"}
    assert_nullable_schema_ref(batch_schema, "optimization_plan_error", "FeedbackJobErrorResponse")
    assert "FeedbackOptimizationBatchExecutionResponse" not in schema["components"]["schemas"]
    assert "FeedbackOptimizationBatchPlanReviewRequest" not in schema["components"]["schemas"]
    batch_attribution_schema = schema["components"]["schemas"]["FeedbackOptimizationBatchAttributionResponse"]
    assert batch_attribution_schema["properties"]["jobs"]["items"] == {"$ref": "#/components/schemas/AgentJobResponse"}
    apply_schema = schema["components"]["schemas"]["OptimizationExecutionApplyResponse"]
    assert apply_schema["properties"]["execution_job"] == {"$ref": "#/components/schemas/OptimizationExecutionJobResponse"}
    assert apply_schema["properties"]["execution_application"] == {"$ref": "#/components/schemas/ExecutionApplicationResponse"}
    assert_nullable_schema_ref(apply_schema, "applied_diff", "AgentVersionDiffResponse")
    plan_task_execution_schema = schema["components"]["schemas"]["FeedbackOptimizationPlanTaskExecuteResponse"]
    assert_nullable_schema_ref(plan_task_execution_schema, "optimization_task", "OptimizationTaskResponse")
    assert_nullable_schema_ref(plan_task_execution_schema, "execution_job", "OptimizationExecutionJobResponse")
    assert_nullable_schema_ref(plan_task_execution_schema, "apply_result", "OptimizationExecutionApplyResponse")
    plan_task_update_schema = schema["components"]["schemas"]["FeedbackOptimizationPlanTaskUpdateResponse"]
    assert_nullable_schema_ref(plan_task_update_schema, "optimization_task", "OptimizationTaskResponse")
    assert_nullable_schema_ref(plan_task_update_schema, "external_item", "ExternalGovernanceItemResponse")
    execution_plan_schema = schema["components"]["schemas"]["OptimizationExecutionPlanOutputResponse"]
    assert_nullable_schema_ref(execution_plan_schema, "planned_diff", "OptimizationExecutionPlannedDiffResponse")
    planned_diff_schema = schema["components"]["schemas"]["OptimizationExecutionPlannedDiffResponse"]
    assert planned_diff_schema["properties"]["files"]["items"] == {"$ref": "#/components/schemas/OptimizationExecutionPlannedDiffFileResponse"}
    execution_job_schema = schema["components"]["schemas"]["OptimizationExecutionJobResponse"]
    assert_nullable_schema_ref(execution_job_schema, "error_json", "FeedbackJobErrorResponse")
    assert "execution_job_id" in execution_job_schema["properties"]
    assert "job_id" not in execution_job_schema["properties"]
    agent_job_schema = schema["components"]["schemas"]["AgentJobResponse"]
    assert_nullable_schema_ref(agent_job_schema, "error_json", "FeedbackJobErrorResponse")
    assert "profile_version" in agent_job_schema["properties"]
    assert "output_schema_version" not in agent_job_schema["properties"]
    assert "AgentVersionRestoreResponse" not in schema["components"]["schemas"]
    assert "AgentVersionManifestResponse" not in schema["components"]["schemas"]
    assert "AgentVersionIncludedRootResponse" not in schema["components"]["schemas"]
    assert "AgentVersionExcludedPathResponse" not in schema["components"]["schemas"]
    assert "AgentVersionSkippedPathResponse" not in schema["components"]["schemas"]
    assert "AgentVersionRelatedDataResponse" not in schema["components"]["schemas"]
    assert "OptimizationProposalReviewResponse" not in schema["components"]["schemas"]
    assert "OptimizationProposalResponse" not in schema["components"]["schemas"]
    evidence_schema = schema["components"]["schemas"]["EvidencePackageResponse"]
    assert evidence_schema["properties"]["source_refs"] == {"$ref": "#/components/schemas/EvidenceSourceRefsResponse"}
    assert evidence_schema["properties"]["included_files"]["items"] == {"$ref": "#/components/schemas/EvidenceIncludedFileResponse"}
    assert evidence_schema["properties"]["redaction"] == {"$ref": "#/components/schemas/EvidenceRedactionResponse"}
    assert evidence_schema["properties"]["completeness"] == {"$ref": "#/components/schemas/EvidenceCompletenessResponse"}
    eval_case_schema = schema["components"]["schemas"]["EvalCaseResponse"]
    assert_nullable_schema_ref(eval_case_schema, "source_summary", "EvalCaseSourceSummaryResponse")
    assert_nullable_schema_ref(eval_case_schema, "attribution_summary", "EvalCaseAttributionSummaryResponse")
    assert_nullable_schema_ref(eval_case_schema, "proposal_summary", "EvalCaseProposalSummaryResponse")
    assert "promotion_status" in eval_case_schema["properties"]
    assert "blocking_policy" in eval_case_schema["properties"]
    eval_generate_schema = schema["components"]["schemas"]["FeedbackEvalCaseGenerateResponse"]
    assert eval_generate_schema["properties"]["results"]["items"] == {"$ref": "#/components/schemas/FeedbackEvalCaseGenerateResultResponse"}
    eval_item_schema = schema["components"]["schemas"]["EvalRunItemResponse"]
    assert eval_item_schema["properties"]["check_results"]["items"] == {"$ref": "#/components/schemas/EvalRunCheckResultResponse"}
    eval_run_schema = schema["components"]["schemas"]["EvalRunResponse"]
    assert eval_run_schema["properties"]["summary"] == {"$ref": "#/components/schemas/EvalRunSummaryResponse"}
    assert "gate_result" in eval_run_schema["properties"]
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
