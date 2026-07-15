from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_improvement_workbench_uses_typed_test_dataset_api_without_dual_ids() -> None:
    api = _read("frontend/src/api/assets.ts")
    workbench = _read("frontend/src/components/ImprovementWorkbench.tsx")
    dataset_state = _read("frontend/src/components/useImprovementTestDataset.ts")
    stage = _read("frontend/src/components/ImprovementStagePanels.tsx")
    context = _read("frontend/src/contextPackage.ts")

    assert not (ROOT / "frontend/src/improvementRegressionAssets.ts").exists()
    assert "adoptTestDataset" in api
    assert "listTestDatasets" in api
    assert "body: JSON.stringify({})" in api
    assert "const dataset = await adoptTestDataset" in workbench
    assert "const [testDatasets, setTestDatasets] = useState<TestDataset[]>([])" in workbench
    assert "testDatasets.find((dataset) => isCurrentTestDataset(dataset" in workbench
    assert "setTestDatasets(datasets)" in workbench
    assert "setTestDatasets((datasets) => [dataset, ...datasets.filter" in workbench
    assert "provenance.normalized_feedback_id === normalizedFeedback.normalized_feedback_id" in dataset_state
    assert "provenance.normalized_feedback_updated_at === normalizedFeedback.updated_at" in dataset_state
    assert "provenance.attribution_id === attribution.attribution_id" in dataset_state
    assert "provenance.attribution_updated_at === attribution.updated_at" in dataset_state
    assert "provenance.optimization_plan_id === optimizationPlan.optimization_plan_id" in dataset_state
    assert "provenance.optimization_plan_updated_at === optimizationPlan.updated_at" in dataset_state
    assert "provenance.execution_id === execution.execution_id" in dataset_state
    assert "provenance.execution_updated_at === execution.updated_at" in dataset_state
    assert "provenance.regression_assessment_id === regressionAssessment.regression_assessment_id" in dataset_state
    assert "provenance.regression_assessment_updated_at === regressionAssessment.updated_at" in dataset_state
    assert "provenance.candidate_agent_version_id === candidateVersion" in dataset_state
    assert "sourceImprovementId: itemId" in workbench
    assert "testDataset?.dataset_id" in stage
    assert "testDataset?.lifecycle_state" in stage
    assert "testDataset.revision" in stage
    assert "testDataset?.cases ?? regressionAssessment?.cases" in stage
    assert "testDatasetError" in workbench + stage
    assert "latestEvalRun" in workbench + stage
    assert "transitionTestDataset" in workbench
    assert "useTestDatasetRevisions(clientConfig, testDataset, testDatasetReloadToken)" in workbench
    assert "listTestDatasetRevisions" in dataset_state
    assert "reloadToken, testDataset?.agent_id, testDataset?.dataset_id, testDataset?.revision" in dataset_state
    assert "finally {\n        setTestDatasetReloadToken((value) => value + 1);" in workbench
    assert 'data-testid="test-dataset-lifecycle-management"' in _read("frontend/src/components/TestDatasetLifecycleControls.tsx")
    assert 'data-testid="test-dataset-load-retry"' in stage
    assert "test_dataset_id" not in workbench + stage + context
    assert "test_dataset_refs" not in context
    assert "test_dataset_ref" in context
    assert "provenance: testDataset.provenance" in context
    assert "cases: testDataset.cases" in context


def test_registry_and_regression_run_consume_active_candidate_bound_dataset() -> None:
    registry = _read("frontend/src/components/AssetRegistry.tsx")
    release = _read("frontend/src/components/ReleaseWorkbench.tsx")
    workbench = _read("frontend/src/components/ImprovementWorkbench.tsx")
    stage_panels = _read("frontend/src/components/ImprovementStagePanels.tsx")
    app = _read("frontend/src/App.tsx")
    runtime_api = _read("frontend/src/api/runtime.ts")

    assert "listTestDatasets" in registry
    assert 'data-testid="test-dataset-structured-fields"' in registry
    assert "dataset.dataset_id" in registry
    assert "dataset.provenance.regression_assessment_id" in registry
    assert registry.count('<option value="test_dataset">') == 1  # 浏览筛选保留，通用创建入口移除。
    assert '<option value="regression">' not in registry
    assert 'regression: "回归"' not in registry
    assert 'dataset.lifecycle_state === "active"' in release
    assert "dataset.provenance.candidate_agent_version_id === candidateVersion" in release
    assert "dataset.provenance.execution_id === executionId" in release
    assert "selectedChangeSet?.execution_job_id" in release
    assert "listTestDatasets(clientConfig, { agentId, sourceImprovementId })" in release
    assert "sourceTestDataset={testDataset}" in workbench
    assert "sourceTestDatasetVersion" in release
    assert "sourceTestDataset.lifecycle_state" in release
    assert "sourceTestDataset.revision" in release
    assert "if (usable.some((dataset) => dataset.dataset_id === current)) return current" not in release
    assert 'data-testid="release-regression-dataset"' in release
    assert "selectedDataset.cases.length" in release
    assert "JSON.stringify({ dataset_id: datasetId })" in runtime_api
    assert "normalizedCaseCount * GOVERNANCE_AGENT_TIMEOUT_MS + 30_000" in runtime_api
    assert "Number.isFinite(caseCount) && caseCount > 0" in runtime_api
    assert "timeoutMs," in runtime_api
    assert "evalCaseIds" not in runtime_api
    assert "changeSet.source_improvement_id === sourceImprovementId" in release
    assert 'data-testid="release-latest-eval-run"' in release
    assert 'data-testid="release-action-retry-cleanup"' in release
    assert "retryAgentChangeSetWorktreeCleanup" in runtime_api
    assert "testReleaseWorkbench" in workbench and "{testReleaseWorkbench}" in stage_panels
    assert "showReleaseWindow" not in app
    assert 'activeWindow === "release"' not in app


def test_improvement_decision_mock_returns_a_typed_dataset_collection() -> None:
    decision_verifier = _read("scripts/verify_improvement_decision_ui.mjs")

    assert '"/api/test-datasets"' in decision_verifier
    assert ".includes(path)) return json(route, []);" in decision_verifier


def test_governance_workbenches_keep_tablet_and_mobile_width_bounded() -> None:
    global_styles = _read("frontend/src/styles.css")
    workbench_styles = _read("frontend/src/improvement-workbench.css")

    assert "min-width: 1180px" not in global_styles
    assert "body {\n  margin: 0;\n  min-width: 320px;" in global_styles
    assert "grid-template-columns: minmax(0, 1fr); overflow: auto;" in workbench_styles
    assert ".iw-stage-panel-grid.test-release .iw-stage-card.is-stage-wide { grid-column: auto; }" in workbench_styles
    assert ".release-stage-workbench .iw-select" in workbench_styles
    assert ".iw-list-panel { min-height: 440px; }" in workbench_styles
    real_flow = _read("scripts/improvement_ui_e2e/real_container_flow.mjs")
    assert "assertCreateControlsFullyVisible" in real_flow
