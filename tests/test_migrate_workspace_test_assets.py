from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.migrate_workspace_test_assets import migrate_workspace_test_assets


def _git(workspace: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return process.stdout.strip()


def _workspace(runtime_root: Path, agent_id: str) -> Path:
    workspace = runtime_root / "data" / "business-agents" / agent_id / "workspace"
    workspace.mkdir(parents=True)
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "AgentGov Test")
    _git(workspace, "config", "user.email", "agentgov-test@example.invalid")
    workspace.joinpath("CLAUDE.md").write_text(f"# {agent_id}\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "initial")
    return workspace


def _bootstrap(tmp_path: Path) -> Path:
    bootstrap = tmp_path / "bootstrap"
    tests_dir = bootstrap / "business-agents" / "security-operations-expert" / "workspace" / "tests"
    tests_dir.mkdir(parents=True)
    tests_dir.joinpath("README.md").write_text("# tests\n", encoding="utf-8")
    tests_dir.joinpath("test_native.py").write_text("def test_native():\n    assert True\n", encoding="utf-8")
    return bootstrap


def test_workspace_test_asset_migration_archives_evals_and_commits_builtin_tests(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    main = _workspace(runtime_root, "main-agent")
    security = _workspace(runtime_root, "security-operations-expert")
    security.joinpath("agent.yaml").write_text(
        "agent:\n  id: security-operations-expert\n  profile: security-operations-expert\n",
        encoding="utf-8",
    )
    _git(security, "add", "-A")
    _git(security, "commit", "-m", "legacy identity")
    evals = main / "evals"
    evals.mkdir()
    evals.joinpath("legacy.json").write_text('{"legacy": true}\n', encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "legacy eval")
    main_before = _git(main, "rev-parse", "HEAD")
    security_before = _git(security, "rev-parse", "HEAD")
    bootstrap = _bootstrap(tmp_path)

    scanned = migrate_workspace_test_assets(
        runtime_root=runtime_root,
        bootstrap_dir=bootstrap,
        apply=False,
    )

    assert {item.agent_id for item in scanned if item.changed} == {"main-agent", "security-operations-expert"}
    assert evals.is_dir()
    assert not (security / "tests").exists()

    applied = migrate_workspace_test_assets(
        runtime_root=runtime_root,
        bootstrap_dir=bootstrap,
        apply=True,
    )
    by_agent = {item.agent_id: item for item in applied}
    archived = Path(str(by_agent["main-agent"].archived_evals_path))
    assert archived.joinpath("legacy.json").read_bytes() == b'{"legacy": true}\n'
    assert not evals.exists()
    assert (security / "tests" / "test_native.py").is_file()
    assert "  id:" not in (security / "agent.yaml").read_text(encoding="utf-8")
    assert "profile: security-operations-expert" in (security / "agent.yaml").read_text(encoding="utf-8")
    assert by_agent["security-operations-expert"].legacy_agent_id_removed is True
    assert by_agent["main-agent"].current_commit_sha != main_before
    assert by_agent["security-operations-expert"].current_commit_sha != security_before
    assert _git(main, "status", "--porcelain=v1") == ""
    assert _git(security, "status", "--porcelain=v1") == ""

    repeated = migrate_workspace_test_assets(
        runtime_root=runtime_root,
        bootstrap_dir=bootstrap,
        apply=True,
    )
    assert not any(item.changed for item in repeated)


def test_workspace_test_asset_migration_refuses_dirty_workspace(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = _workspace(runtime_root, "ordinary-agent")
    workspace.joinpath("CLAUDE.md").write_text("# dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="dirty business Agent Workspace"):
        migrate_workspace_test_assets(
            runtime_root=runtime_root,
            bootstrap_dir=_bootstrap(tmp_path),
            apply=True,
        )

    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == "# dirty\n"
    assert not (workspace / "tests").exists()
    assert _git(workspace, "status", "--porcelain=v1") == "M CLAUDE.md"


def test_workspace_test_asset_migration_preflights_all_write_permissions(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    writable = _workspace(runtime_root, "main-agent")
    evals = writable / "evals"
    evals.mkdir()
    evals.joinpath("legacy.json").write_text("{}\n", encoding="utf-8")
    _git(writable, "add", "-A")
    _git(writable, "commit", "-m", "legacy eval")
    blocked = _workspace(runtime_root, "security-operations-expert")
    blocked.chmod(0o555)
    try:
        with pytest.raises(RuntimeError, match="non-writable business Agent Workspace"):
            migrate_workspace_test_assets(
                runtime_root=runtime_root,
                bootstrap_dir=_bootstrap(tmp_path),
                apply=True,
            )
    finally:
        blocked.chmod(0o755)

    assert evals.joinpath("legacy.json").is_file()
    assert not (blocked / "tests").exists()
    assert _git(writable, "log", "-1", "--pretty=%s") == "legacy eval"


def test_workspace_test_asset_migration_archives_only_legacy_generated_weak_tests(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = _workspace(runtime_root, "ordinary-agent")
    tests_dir = workspace / "tests"
    tests_dir.mkdir()
    generated = tests_dir / "test_generated.py"
    generated.write_text(
        "# Generated from a confirmed AgentGov regression test design.\n"
        "# Agent developers may add stronger tests in new files; this file is immutable once committed.\n\n"
        "def test_generated(agent):\n"
        "    expected_behavior = 'answer'\n"
        "    checkpoints = ['non-empty']\n"
        "    result = agent.invoke('prompt')\n"
        "    assert result.text.strip(), expected_behavior\n"
        "    assert all(checkpoint.strip() for checkpoint in checkpoints)\n",
        encoding="utf-8",
    )
    developer_test = tests_dir / "test_developer.py"
    developer_test.write_text(
        "def test_developer(agent):\n"
        "    result = agent.invoke('outside generated template')\n"
        "    assert 'specific' in result.text\n",
        encoding="utf-8",
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "legacy testkit calls")
    bootstrap = _bootstrap(tmp_path)

    scanned = migrate_workspace_test_assets(runtime_root=runtime_root, bootstrap_dir=bootstrap, apply=False)
    result = next(item for item in scanned if item.agent_id == "ordinary-agent")
    assert result.legacy_generated_test_files_archived == ("tests/test_generated.py",)
    assert generated.is_file()

    applied = migrate_workspace_test_assets(runtime_root=runtime_root, bootstrap_dir=bootstrap, apply=True)
    result = next(item for item in applied if item.agent_id == "ordinary-agent")
    assert result.changed is True
    assert not generated.exists()
    assert "outside generated template" in developer_test.read_text(encoding="utf-8")
    archives = list((runtime_root / "data" / "archived-legacy-test-assets" / "ordinary-agent").glob("*/generated-pytest/tests/test_generated.py"))
    assert len(archives) == 1
    assert "expected_behavior" in archives[0].read_text(encoding="utf-8")
    assert _git(workspace, "status", "--porcelain=v1") == ""

    repeated = migrate_workspace_test_assets(runtime_root=runtime_root, bootstrap_dir=bootstrap, apply=True)
    assert not any(item.changed for item in repeated)
