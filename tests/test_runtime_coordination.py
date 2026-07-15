from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import app.runtime.runtime_initialization as runtime_initialization
import pytest
from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_coordination import (
    RuntimeCoordinationPaths,
    prepare_runtime_contract,
    runtime_contract_status,
)
from app.runtime.runtime_initialization import RuntimeInitializationError
from app.runtime.settings import AppSettings

_GENERIC_MUTATION_ASK = [
    "mcp__*__*write*",
    "mcp__*__*update*",
    "mcp__*__*delete*",
    "mcp__*__*block*",
    "mcp__*__*isolate*",
    "mcp__*__*disable*",
    "mcp__*__*kill*",
    "mcp__*__*quarantine*",
]


def _settings(tmp_path: Path) -> AppSettings:
    root = tmp_path / "runtime"
    return AppSettings(
        _env_file=None,
        DATA_DIR=root / "data",
        GOVERNOR_WORKSPACE_DIR=root / "governor-workspace",
        GOVERNOR_CLAUDE_ROOT=root / "claude-roots" / "governor",
        RUNTIME_VOLUME_MODE="local-debug",
    )


def _template(tmp_path: Path) -> Path:
    root = tmp_path / "seeds"
    workspace = root / "data" / "business-agents" / "main-agent" / "workspace"
    settings = {
        "permissions": {
            "allow": ["Read(./**)", "Glob", "Grep", "Skill"],
            "ask": ["Bash(*)", "Edit(./**)", "Write(./**)", *_GENERIC_MUTATION_ASK],
            "deny": [],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "enableWeakerNestedSandbox": False,
            "allowUnsandboxedCommands": False,
        },
    }
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (workspace / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    (workspace / "hooks").mkdir()
    (workspace / "hooks" / "pre_tool_guard.py").write_text("# managed test hook\n", encoding="utf-8")
    return root


def _prepare(settings: AppSettings, template: Path, env: dict[str, str] | None = None):
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    with advisory_lock(paths.phase_lock, mode="exclusive") as lease:
        return prepare_runtime_contract(
            settings=settings,
            template_dir=template,
            env=env or {},
            lease=lease,
        )


def _main_store(settings: AppSettings) -> GitAgentVersionStore:
    layout = business_agent_layout(settings.data_dir, "main-agent")
    return GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )


def _remove_managed_ask(settings: AppSettings, *, commit: bool) -> GitAgentVersionStore:
    store = _main_store(settings)
    path = settings.main_workspace_dir / ".claude" / "settings.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["permissions"]["ask"].remove(_GENERIC_MUTATION_ASK[-1])
    path.write_text(json.dumps(payload), encoding="utf-8")
    if commit:
        store.create_snapshot(reason="historical_policy", note="historical managed policy")
    return store


def test_runtime_receipt_is_idempotent_and_bound_to_environment(tmp_path):
    settings = _settings(tmp_path)
    template = _template(tmp_path)

    first = _prepare(settings, template)
    status = runtime_contract_status(settings=settings, template_dir=template, env={})
    second = _prepare(settings, template)

    assert status.valid is True
    assert status.reason == "ok"
    assert second.volume_id == first.volume_id
    assert (
        runtime_contract_status(
            settings=settings,
            template_dir=template,
            env={"MCP_SERVER_URL": "http://different.example/mcp"},
        ).reason
        == "desired_digest_mismatch"
    )
    assert (
        runtime_contract_status(
            settings=settings,
            template_dir=template,
            env={"CLAUDE_ALLOWED_NETWORK_DOMAINS": "soc.internal"},
        ).reason
        == "desired_digest_mismatch"
    )


def test_runtime_receipt_is_bound_to_local_runtime_root(tmp_path):
    settings = _settings(tmp_path / "first")
    template = _template(tmp_path)
    _prepare(settings, template)
    relocated = _settings(tmp_path / "relocated")
    relocated_root = relocated.data_dir.parent
    relocated_root.parent.mkdir(parents=True, exist_ok=True)
    settings.data_dir.parent.rename(relocated_root)

    status = runtime_contract_status(settings=relocated, template_dir=template, env={})

    assert status.reason == "desired_digest_mismatch"


def test_clean_historical_workspace_is_migrated_with_git_snapshot(tmp_path):
    settings = _settings(tmp_path)
    template = _template(tmp_path)
    _prepare(settings, template)
    expected_hook = (template / "data" / "business-agents" / "main-agent" / "workspace" / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8")
    hook_path = settings.main_workspace_dir / "hooks" / "pre_tool_guard.py"
    hook_path.unlink()
    store = _remove_managed_ask(settings, commit=True)
    historical_head = store.current_commit_sha()

    receipt = _prepare(settings, template)

    assert receipt.agent_commits["main-agent"] == store.current_commit_sha()
    assert store.current_commit_sha() != historical_head
    assert store.workspace_changes() == []
    assert hook_path.read_text(encoding="utf-8") == expected_hook
    assert stat.S_IMODE(hook_path.stat().st_mode) == 0o600


def test_managed_policy_migration_failure_restores_historical_git_snapshot(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    template = _template(tmp_path)
    _prepare(settings, template)
    hook_path = settings.main_workspace_dir / "hooks" / "pre_tool_guard.py"
    hook_path.unlink()
    store = _remove_managed_ask(settings, commit=True)
    historical_head = store.current_commit_sha()
    real_write = runtime_initialization._write_policy_changes

    def write_then_fail(workspace, changes):
        real_write(workspace, changes)
        raise OSError("injected managed migration failure")

    monkeypatch.setattr(runtime_initialization, "_write_policy_changes", write_then_fail)

    with pytest.raises(OSError, match="injected managed migration failure"):
        _prepare(settings, template)

    assert store.current_commit_sha() == historical_head
    assert store.workspace_changes() == []
    assert not hook_path.exists()
    assert not (RuntimeCoordinationPaths.from_data_dir(settings.data_dir).root / "migration-journal.json").exists()


def test_dirty_workspace_blocks_managed_policy_migration(tmp_path):
    settings = _settings(tmp_path)
    template = _template(tmp_path)
    _prepare(settings, template)
    _remove_managed_ask(settings, commit=False)

    with pytest.raises(RuntimeInitializationError, match="Dirty workspace blocks"):
        _prepare(settings, template)


def test_open_change_set_blocks_migration_before_head_changes(tmp_path):
    settings = _settings(tmp_path)
    template = _template(tmp_path)
    _prepare(settings, template)
    store = _remove_managed_ask(settings, commit=True)
    historical_head = store.current_commit_sha()
    settings.runtime_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.runtime_db_path) as connection:
        connection.execute("CREATE TABLE agent_change_sets (change_set_id TEXT, agent_id TEXT, status TEXT)")
        connection.execute(
            "INSERT INTO agent_change_sets VALUES (?, ?, ?)",
            ("agc-open", "main-agent", "draft"),
        )

    with pytest.raises(RuntimeInitializationError, match="Open change sets block"):
        _prepare(settings, template)

    assert store.current_commit_sha() == historical_head


def test_shared_runtime_lease_rejects_exclusive_maintenance_process(tmp_path):
    lock_path = tmp_path / "runtime-phase.lock"
    script = """
from pathlib import Path
from app.runtime.advisory_lock import AdvisoryLockBusy, advisory_lock
try:
    with advisory_lock(Path(__import__('sys').argv[1]), mode='exclusive', blocking=False):
        raise SystemExit(2)
except AdvisoryLockBusy:
    raise SystemExit(0)
"""
    with advisory_lock(lock_path, mode="shared"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
    assert result.returncode == 0
