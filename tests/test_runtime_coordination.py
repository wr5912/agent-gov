from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.managed_agent_policy import ManagedAgentPolicyError
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_coordination import (
    RuntimeCoordinationPaths,
    prepare_runtime_contract,
    runtime_contract_status,
)
from app.runtime.settings import AppSettings


def _settings(tmp_path: Path, *, initialize_workspace: bool = True) -> AppSettings:
    root = tmp_path / "runtime"
    settings = AppSettings(
        _env_file=None,
        AGENTGOV_RUNTIME_SHARED_SECRET="test-runtime-shared-secret",
        DATA_DIR=root / "data",
        GOVERNOR_WORKSPACE_DIR=root / "governor-workspace",
        RUNTIME_VOLUME_MODE="local-debug",
    )
    if initialize_workspace:
        workspace = settings.default_workspace_dir
        workspace.mkdir(parents=True)
        (workspace / "AGENT.md").write_text("# Business Agent\n", encoding="utf-8")
        (workspace / "agent.yaml").write_text(_workspace_manifest(), encoding="utf-8")
    return settings


def _workspace_manifest() -> str:
    return (
        "schema_version: 1\n"
        "agent:\n"
        "  id: security-operations-expert\n"
        "  runtime: agentscope\n"
        "  runtime_contract: agentscope-app/2.0.8\n"
        "workspace_policy:\n"
        "  fail_closed: true\n"
        "  immutable_harness: true\n"
        "  allow_for_run: false\n"
        "  allowed_tools: [Read(./**), Glob, Grep, Skill]\n"
        "  denied_tools: [Read(./.env)]\n"
    )


def _bootstrap(tmp_path: Path) -> Path:
    root = tmp_path / "runtime-bootstrap"
    workspace = root / "business-agents" / "security-operations-expert" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "AGENT.md").write_text("# Business Agent\n", encoding="utf-8")
    (workspace / "agent.yaml").write_text(_workspace_manifest(), encoding="utf-8")
    governor = root / "governor-workspace"
    governor.mkdir()
    (governor / "AGENT.md").write_text("# Governor\n", encoding="utf-8")
    (governor / "agent.yaml").write_text(
        _workspace_manifest().replace("security-operations-expert", "governor"),
        encoding="utf-8",
    )
    return root


def _prepare(settings: AppSettings, bootstrap: Path, env: dict[str, str] | None = None):
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    with advisory_lock(paths.phase_lock, mode="exclusive") as lease:
        return prepare_runtime_contract(
            settings=settings,
            bootstrap_dir=bootstrap,
            env=env or {},
            lease=lease,
        )


def _default_store(settings: AppSettings) -> GitAgentVersionStore:
    layout = business_agent_layout(settings.data_dir, DEFAULT_BUSINESS_AGENT_ID)
    return GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )


def _narrow_allowed_tools(settings: AppSettings, *, commit: bool) -> GitAgentVersionStore:
    store = _default_store(settings)
    workspace = settings.default_workspace_dir
    path = workspace / "agent.yaml"
    if commit:
        base = str(store.current_commit_sha() or store.ensure_bootstrap()["agent_version_id"])
        candidate = store.create_worktree("historical-policy", base_ref=base)
        path = candidate.worktree_path / "agent.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(", Skill", ""), encoding="utf-8")
    if commit:
        commit_sha = store.commit_worktree(candidate.worktree_path, message="historical managed policy")
        store.publish_commit(commit_sha, tag_name=f"historical-policy-{commit_sha[:12]}", message="historical managed policy")
    return store


def test_runtime_receipt_is_idempotent_and_not_bound_to_runtime_endpoint_env(tmp_path):
    settings = _settings(tmp_path)
    template = _bootstrap(tmp_path)

    first = _prepare(settings, template)
    receipt_payload = json.loads(RuntimeCoordinationPaths.from_data_dir(settings.data_dir).receipt.read_text(encoding="utf-8"))
    status = runtime_contract_status(settings=settings, bootstrap_dir=template, env={})
    second = _prepare(settings, template)

    assert set(receipt_payload) == {
        "completed_at",
        "contract",
        "desired_digest",
        "runtime_mode",
        "volume_id",
    }
    assert status.valid is True
    assert status.reason == "ok"
    assert status.workspace_validation_digest
    assert second.volume_id == first.volume_id
    assert runtime_contract_status(
        settings=settings,
        bootstrap_dir=template,
        env={"MCP_SERVER_URL": "http://different.example/mcp"},
    ).valid
    assert runtime_contract_status(
        settings=settings,
        bootstrap_dir=template,
        env={"AGENTSCOPE_RUNTIME_URL": "http://different-runtime.example"},
    ).valid


def test_runtime_receipt_is_bound_to_local_runtime_root(tmp_path):
    settings = _settings(tmp_path / "first")
    template = _bootstrap(tmp_path)
    _prepare(settings, template)
    relocated = _settings(tmp_path / "relocated", initialize_workspace=False)
    relocated_root = relocated.data_dir.parent
    relocated_root.parent.mkdir(parents=True, exist_ok=True)
    settings.data_dir.parent.rename(relocated_root)

    status = runtime_contract_status(settings=relocated, bootstrap_dir=template, env={})

    assert status.reason == "desired_digest_mismatch"


def test_clean_historical_workspace_is_not_rewritten_or_recommitted(tmp_path):
    settings = _settings(tmp_path)
    template = _bootstrap(tmp_path)
    _prepare(settings, template)
    store = _narrow_allowed_tools(settings, commit=True)
    historical_head = store.current_commit_sha()
    settings_path = settings.default_workspace_dir / "agent.yaml"
    historical_bytes = settings_path.read_bytes()

    _prepare(settings, template)

    assert store.current_commit_sha() == historical_head
    assert store.workspace_changes() == []
    assert settings_path.read_bytes() == historical_bytes


def test_invalid_historical_workspace_fails_read_only_validation_without_rewrite(tmp_path):
    settings = _settings(tmp_path)
    template = _bootstrap(tmp_path)
    _prepare(settings, template)
    store = _default_store(settings)
    historical_head = store.current_commit_sha()
    settings_path = settings.default_workspace_dir / "agent.yaml"
    settings_path.write_text("{", encoding="utf-8")

    with pytest.raises(ManagedAgentPolicyError, match="invalid_manifest"):
        _prepare(settings, template)

    assert store.current_commit_sha() == historical_head
    assert settings_path.read_text(encoding="utf-8") == "{"
    assert {str(item["path"]) for item in store.workspace_changes()} == {"agent.yaml"}


def test_dirty_workspace_does_not_block_receipt_refresh_or_get_committed(tmp_path):
    settings = _settings(tmp_path)
    template = _bootstrap(tmp_path)
    _prepare(settings, template)
    store = _narrow_allowed_tools(settings, commit=False)
    historical_head = store.current_commit_sha()
    settings_path = settings.default_workspace_dir / "agent.yaml"
    dirty_bytes = settings_path.read_bytes()

    _prepare(settings, template)

    assert store.current_commit_sha() == historical_head
    assert settings_path.read_bytes() == dirty_bytes
    assert {str(item["path"]) for item in store.workspace_changes()} == {"agent.yaml"}


def test_open_change_set_does_not_trigger_workspace_migration(tmp_path):
    settings = _settings(tmp_path)
    template = _bootstrap(tmp_path)
    _prepare(settings, template)
    store = _narrow_allowed_tools(settings, commit=True)
    historical_head = store.current_commit_sha()
    settings.runtime_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.runtime_db_path) as connection:
        connection.execute("CREATE TABLE agent_change_sets (change_set_id TEXT, agent_id TEXT, status TEXT)")
        connection.execute(
            "INSERT INTO agent_change_sets VALUES (?, ?, ?)",
            ("agc-open", DEFAULT_BUSINESS_AGENT_ID, "draft"),
        )

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
