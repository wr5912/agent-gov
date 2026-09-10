from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from app.runtime import published_harness_preparation as preparation
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.settings import AppSettings
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.store import RuntimeObjectNotFound, RuntimeStateConflict, harness_digest


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "data",
        GOVERNOR_WORKSPACE_DIR=tmp_path / "governor",
        RUNTIME_CANDIDATES_DIR=tmp_path / "candidates",
        RUNTIME_VOLUME_MODE="local-debug",
    )


def _workspace(settings: AppSettings, agent_id: str = "business") -> Path:
    workspace = business_agent_layout(settings.data_dir, agent_id).workspace
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text(
        f"agent: {{id: {agent_id}, runtime: agentscope}}\nsession: {{permission_mode: default}}\n",
        encoding="utf-8",
    )
    (workspace / "AGENT.md").write_bytes(b"# Existing workspace\r\nkeep these bytes\r\n")
    subagent = workspace / "subagents" / "specialist"
    subagent.mkdir(parents=True)
    (subagent / "agent.yaml").write_text("agent: {runtime: agentscope}\n", encoding="utf-8")
    (subagent / "AGENT.md").write_text("Specialist\n", encoding="utf-8")
    return workspace


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace_bytes(workspace: Path) -> dict[str, bytes]:
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(workspace).parts
    }


def test_fresh_and_repeated_preparation_preserve_bytes_and_do_not_create_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = _workspace(settings)
    original = _workspace_bytes(workspace)

    assert preparation.prepare_published_harnesses(settings) == 1
    snapshot_root = next(settings.runtime_candidates_dir.glob("published-*"))
    marker = snapshot_root / "snapshot.json"
    identity = json.loads(marker.read_text())
    first_stamp = marker.stat().st_mtime_ns
    commit = _git(workspace, "rev-parse", "HEAD")
    assert identity["agent_version_id"] == commit
    assert identity["harness_digest"] == harness_digest(workspace)
    assert _workspace_bytes(snapshot_root / "workspace") == original
    assert not (snapshot_root / "workspace" / "AGENT.md").stat().st_mode & 0o222
    assert not settings.runtime_db_path.exists()

    assert preparation.prepare_published_harnesses(settings) == 1
    assert _workspace_bytes(workspace) == original
    assert _git(workspace, "rev-parse", "HEAD") == commit
    assert marker.stat().st_mtime_ns == first_stamp
    assert len(list(settings.runtime_candidates_dir.glob("published-*"))) == 1
    assert not settings.runtime_db_path.exists()


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_dirty_workspace_is_rejected_without_commit_or_snapshot_change(tmp_path: Path, change: str) -> None:
    settings = _settings(tmp_path)
    workspace = _workspace(settings)
    preparation.prepare_published_harnesses(settings)
    old_commit = _git(workspace, "rev-parse", "HEAD")
    target = workspace / ("AGENT.md" if change == "tracked" else "unpublished.txt")
    target.write_text("unpublished change\n", encoding="utf-8")

    with pytest.raises(RuntimeStateConflict, match="clean governed Git"):
        preparation.prepare_published_harnesses(settings)

    assert target.read_text() == "unpublished change\n"
    assert _git(workspace, "rev-parse", "HEAD") == old_commit
    assert len(list(settings.runtime_candidates_dir.glob("published-*"))) == 1


def test_new_committed_version_creates_another_immutable_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = _workspace(settings)
    preparation.prepare_published_harnesses(settings)
    first = next(settings.runtime_candidates_dir.glob("published-*"))
    original = _workspace_bytes(first / "workspace")
    (workspace / "AGENT.md").write_text("new published instructions\n", encoding="utf-8")
    _git(workspace, "add", "AGENT.md")
    _git(workspace, "commit", "-m", "new governed version")

    assert preparation.prepare_published_harnesses(settings) == 1

    snapshots = list(settings.runtime_candidates_dir.glob("published-*"))
    assert len(snapshots) == 2
    assert _workspace_bytes(first / "workspace") == original
    second = next(path for path in snapshots if path != first)
    assert (second / "workspace/AGENT.md").read_text() == "new published instructions\n"


def test_candidate_sources_and_worktrees_are_not_discovered_or_rewritten(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = _workspace(settings)
    preparation.prepare_published_harnesses(settings)
    layout = business_agent_layout(settings.data_dir, "business")
    candidate_worktree = layout.version_base / "worktrees" / "unpublished"
    candidate_worktree.mkdir(parents=True)
    (candidate_worktree / "AGENT.md").write_text("unpublished candidate\n", encoding="utf-8")
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )
    candidate = PublishedHarnessSnapshotStore(settings.runtime_candidates_dir).materialize_candidate(
        version_store=versions,
        agent_id="business",
        agent_version_id=_git(workspace, "rev-parse", "HEAD"),
        expected_digest=harness_digest(workspace),
        isolation_key="existing-candidate",
    )
    candidate_stamp = candidate.workspace.stat().st_mtime_ns

    assert preparation.prepare_published_harnesses(settings) == 1
    assert candidate.workspace.stat().st_mtime_ns == candidate_stamp
    assert (candidate_worktree / "AGENT.md").read_text() == "unpublished candidate\n"
    assert not (candidate_worktree / ".git").exists()
    assert len(list(settings.runtime_candidates_dir.glob("published-*"))) == 1


@pytest.mark.parametrize("deleted_at,provision_state", [("deleted", "ready"), (None, "reserved")])
def test_deleted_or_incomplete_registry_agent_is_not_revived(tmp_path: Path, deleted_at, provision_state) -> None:
    settings = _settings(tmp_path)
    workspace = _workspace(settings)
    with sqlite3.connect(settings.runtime_db_path) as connection:
        connection.execute("CREATE TABLE agent_registry (agent_id TEXT, deleted_at TEXT, provision_state TEXT)")
        connection.execute("INSERT INTO agent_registry VALUES (?, ?, ?)", ("business", deleted_at, provision_state))
    original_db = settings.runtime_db_path.read_bytes()

    assert preparation.prepare_published_harnesses(settings) == 0

    assert not (workspace / ".git").exists()
    assert not settings.runtime_candidates_dir.exists()
    assert settings.runtime_db_path.read_bytes() == original_db


def test_tampered_existing_snapshot_fails_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _workspace(settings)
    preparation.prepare_published_harnesses(settings)
    prompt = next(settings.runtime_candidates_dir.glob("published-*")) / "workspace/AGENT.md"
    prompt.chmod(0o600)
    prompt.write_text("tampered snapshot\n", encoding="utf-8")

    with pytest.raises(RuntimeStateConflict, match="read-only|immutable tuple"):
        preparation.prepare_published_harnesses(settings)


def test_invalid_harness_is_rejected_before_git_initialization(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = _workspace(settings)
    (workspace / "agent.yaml").write_text("agent: {runtime: unsupported}\n", encoding="utf-8")

    with pytest.raises(RuntimeObjectNotFound, match="must declare the AgentScope runtime"):
        preparation.prepare_published_harnesses(settings)

    assert not (workspace / ".git").exists()
    assert not settings.runtime_candidates_dir.exists()
    assert not settings.runtime_db_path.exists()


def test_cli_reports_counts_and_never_echoes_private_failure_details(tmp_path: Path, monkeypatch, capsys) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(preparation, "get_settings", lambda: settings)
    assert preparation.main() == 0
    assert capsys.readouterr().out == "published_harnesses_prepared=0\n"

    def fail(_settings):
        raise ValueError("private-path secret-value")

    monkeypatch.setattr(preparation, "prepare_published_harnesses", fail)
    assert preparation.main() == 1
    output = capsys.readouterr()
    assert output.err == "published_harness_preparation_failed=ValueError\n"
    assert "private-path" not in output.out + output.err
    assert "secret-value" not in output.out + output.err
