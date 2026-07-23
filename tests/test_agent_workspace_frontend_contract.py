from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_settings_workspace_flow_uses_package_only_creation_and_exposes_receipts() -> None:
    settings = _read("frontend/src/components/SettingsModal.tsx")
    management = _read("frontend/src/components/BusinessAgentManagementPanel.tsx")
    table = _read("frontend/src/components/BusinessAgentTable.tsx")
    action_menu = _read("frontend/src/components/AgentActionMenu.tsx")
    drawer = _read("frontend/src/components/AgentWorkspaceImportDrawer.tsx")
    request = _read("frontend/src/api/request.ts")
    browser_verification = _read("scripts/verify_agent_workspace_settings.mjs")

    assert "<BusinessAgentManagementPanel" in settings
    assert "AgentCreateForm" not in settings
    assert not (ROOT / "frontend/src/components/AgentCreateForm.tsx").exists()
    assert not (ROOT / "frontend/src/components/AgentCreateSourceSelect.tsx").exists()
    assert not (ROOT / "frontend/src/components/AgentWorkspacePackagePanel.tsx").exists()
    assert not (ROOT / "frontend/src/components/AgentWorkspaceInventory.tsx").exists()
    assert table.count('data-testid="settings-agent-table"') == 1
    assert 'data-testid="settings-agent-import-open"' in management
    assert 'data-testid="settings-agent-actions-trigger"' in table
    assert 'data-testid="settings-agent-actions-menu"' in action_menu
    assert 'data-testid="settings-agent-export"' in action_menu
    assert 'data-testid="settings-agent-overwrite"' in action_menu
    assert 'data-testid="settings-agent-delete"' in action_menu
    assert "createPortal" in action_menu
    assert 'aria-haspopup="menu"' in table
    assert "Agent ID ${targetId} 已存在" in management
    assert 'testId="settings-agent-import-drawer"' in drawer
    assert 'data-testid="settings-workspace-import-submit"' in drawer
    assert "导入并创建" in drawer
    assert "确认覆盖" in drawer
    assert "closeDisabled={busy}" in drawer
    assert 'data-testid="settings-workspace-import-receipt"' in drawer
    assert "receipt.previous_commit_sha" in drawer
    assert "receipt.current_commit_sha" in drawer
    assert "receipt.package_sha256" in drawer
    assert "receipt.tree_sha256" in drawer
    assert "receipt?.rollback_target_commit_sha" in drawer
    assert "setLastImport(result)" in management
    assert "changeAgentId" in management
    assert "runner.clearFeedback()" in management
    assert 'data-testid="settings-workspace-operation-feedback"' in drawer
    assert "data-operation={notice.operation}" in drawer
    assert 'fileInput.current.value = ""' in management
    assert "settings-workspace-agent-list" not in management + table + action_menu + drawer + settings
    assert 'data-testid="settings-agent-import"' not in management + table + action_menu + drawer + settings
    assert 'data-testid="settings-error"' in settings
    assert "`[${errorCode}] ${detail}`" in request
    assert "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH" in browser_verification
    assert "source-agent" in browser_verification
    assert "workspace-agent" in browser_verification
    assert "完全一致后重新打包" in browser_verification
    assert len(settings.splitlines()) < 800
    assert len(management.splitlines()) < 800
