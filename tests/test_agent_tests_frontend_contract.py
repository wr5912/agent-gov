from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_workspace_tests_are_the_only_active_business_agent_test_asset() -> None:
    asset_api = _read("frontend/src/api/assets.ts")
    registry = _read("frontend/src/components/AssetRegistry.tsx")
    stage = _read("frontend/src/components/ImprovementStagePanels.tsx")
    context = _read("frontend/src/contextPackage.ts")

    assert "TestDataset" not in asset_api + registry + stage + context
    assert "test-dataset" not in asset_api + registry + stage + context
    assert not (ROOT / "frontend/src/components/useImprovementTestDataset.ts").exists()
    assert not (ROOT / "frontend/src/components/TestDatasetLifecycleControls.tsx").exists()
    assert 'data-testid="confirm-regression-tests"' in stage
    assert "确认待发布变更" in stage
    assert "确认并生成测试文件" not in stage
    assert "createAgentChangeSetTestRun" not in stage
    assert "tests/test_*.py" in stage + context
    assert "workspace_tests" in context
    assert "regression_test_design" in context


def test_release_workbench_runs_fixed_commit_bound_platform_tests() -> None:
    release = _read("frontend/src/components/ReleaseWorkbench.tsx")
    runtime_api = _read("frontend/src/api/runtime.ts")

    assert "inspectAgentTestSuite" in release
    assert "createAgentChangeSetTestRun" in release
    assert "listAgentTestRuns" in release
    assert "currentTestRun = latestExactRun(testRuns, selectedChangeSet?.candidate_commit_sha)" in release
    assert 'data-testid="release-action-run-tests"' in release
    assert 'data-testid="release-action-cancel-tests"' in release
    assert 'data-testid="release-test-output"' in release
    assert "只认可当前待发布 commit 的运行记录" in release
    assert "修复前版本" in release
    assert "待发布版本" in release
    assert "/test-suite" in runtime_api
    assert "/test-runs" in runtime_api
    assert "/api/agent-test-runs" in runtime_api
    assert "/api/agent-change-sets/${encodeURIComponent(changeSetId)}/test-runs" in runtime_api
    assert "regression-runs" not in runtime_api


def test_agent_settings_show_workspace_test_status_and_import_audit() -> None:
    management = _read("frontend/src/components/BusinessAgentManagementPanel.tsx")
    table = _read("frontend/src/components/BusinessAgentTable.tsx")
    drawer = _read("frontend/src/components/AgentWorkspaceImportDrawer.tsx")

    assert "inspectAgentTestSuite" in management
    assert "listAgentTestRuns" in management
    assert 'data-testid="settings-agent-test-status"' in table
    assert "suite?.commit_sha" in table
    assert "Workspace / 测试" in table
    assert "receipt.import_record_id" in drawer
    assert "receipt.test_suite_status" in drawer


def test_feedback_release_hides_force_publish_and_keeps_historical_audit() -> None:
    release = _read("frontend/src/components/ReleaseWorkbench.tsx")

    assert 'data-testid="release-action-force"' not in release
    assert 'data-testid="release-force-reason"' not in release
    assert "force: true" not in release
    assert "release.force_published" in release
    assert "测试条件被管理员绕过" in release
    assert "release.force_publication_blocker" in release
    assert "release.force_publish_reason" in release
    assert "release.operator" in release


def test_governance_workbenches_keep_tablet_and_mobile_width_bounded() -> None:
    global_styles = _read("frontend/src/styles.css")
    workbench_styles = _read("frontend/src/improvement-workbench.css")

    assert "min-width: 1180px" not in global_styles
    assert "body {\n  margin: 0;\n  min-width: 320px;" in global_styles
    assert "grid-template-columns: minmax(0, 1fr); overflow: auto;" in workbench_styles
    assert ".iw-stage-panel-grid.test-release .iw-stage-card.is-stage-wide { grid-column: auto; }" in workbench_styles
    assert ".release-stage-workbench .iw-select" in workbench_styles
    assert ".iw-list-panel { min-height: 440px; }" in workbench_styles
    assert "assertCreateDrawerFullyVisible" in _read("scripts/improvement_ui_e2e/real_container_flow.mjs")
