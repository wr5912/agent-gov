from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_settings_workspace_flow_exposes_source_receipt_restore_and_failure_details() -> None:
    settings = _read("frontend/src/components/SettingsModal.tsx")
    create_form = _read("frontend/src/components/AgentCreateForm.tsx")
    source = _read("frontend/src/components/AgentCreateSourceSelect.tsx")
    workspace = _read("frontend/src/components/AgentWorkspacePackagePanel.tsx")
    request = _read("frontend/src/api/request.ts")

    assert "<AgentCreateForm" in settings
    assert "<AgentCreateSourceSelect" in create_form
    assert "<AgentWorkspacePackagePanel" in settings
    assert 'data-testid="settings-agent-create-source"' in source
    assert "seed workspace 内容原样复制，内部身份表述不会自动修改" in source
    assert 'data-testid="settings-workspace-import-submit"' in workspace
    assert 'data-testid="settings-agent-export"' in workspace
    assert 'data-testid="settings-workspace-import-receipt"' in workspace
    assert "receipt.previous_commit_sha" in workspace
    assert "receipt.current_commit_sha" in workspace
    assert "receipt.package_sha256" in workspace
    assert "receipt.tree_sha256" in workspace
    assert "lastImport?.rollback_target_commit_sha" in workspace
    assert "setLastImport(result)" in workspace
    assert "clearImportContext()" in workspace
    assert "changeAgentId" in workspace
    assert "runner.clearFeedback()" in workspace
    assert 'data-testid="settings-workspace-operation-feedback"' in workspace
    assert "data-operation={notice.operation}" in workspace
    assert 'fileInput.current.value = ""' in workspace
    assert 'data-testid="settings-error"' in settings
    assert "`[${errorCode}] ${detail}`" in request
    assert len(settings.splitlines()) < 800
