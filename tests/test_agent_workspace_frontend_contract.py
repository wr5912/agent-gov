from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_settings_workspace_flow_uses_package_only_creation_and_exposes_receipts() -> None:
    settings = _read("frontend/src/components/SettingsModal.tsx")
    workspace = _read("frontend/src/components/AgentWorkspacePackagePanel.tsx")
    inventory = _read("frontend/src/components/AgentWorkspaceInventory.tsx")
    request = _read("frontend/src/api/request.ts")

    assert "<AgentWorkspacePackagePanel" in settings
    assert "AgentCreateForm" not in settings
    assert not (ROOT / "frontend/src/components/AgentCreateForm.tsx").exists()
    assert not (ROOT / "frontend/src/components/AgentCreateSourceSelect.tsx").exists()
    assert "已有 ID 将覆盖；新 ID 将创建" in workspace
    assert "仅创建新 Agent 时必填" in workspace
    assert 'data-testid="settings-workspace-import-submit"' in workspace
    assert "<AgentWorkspaceInventory" in workspace
    assert 'data-testid="settings-agent-export"' in inventory
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
